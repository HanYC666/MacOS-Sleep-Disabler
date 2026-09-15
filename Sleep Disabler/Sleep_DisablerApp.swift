//
//  Sleep_DisablerApp.swift
//  Sleep Disabler
//

import SwiftUI
import AppKit
import ServiceManagement
import Combine
import Darwin
import IOKit
import IOKit.ps

// DisplayServices is private, so resolve the optional brightness functions at
// runtime. A missing symbol disables only the lid dimmer and never prevents the
// Intel or Apple Silicon app from launching.
private enum DisplayBrightness {
    typealias Getter = @convention(c) (CGDirectDisplayID, UnsafeMutablePointer<Float>) -> Int32
    typealias Setter = @convention(c) (CGDirectDisplayID, Float) -> Int32

    private static let handle = dlopen(
        "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices",
        RTLD_LAZY
    )

    static let getBrightness: Getter? = handle.flatMap { handle in
        dlsym(handle, "DisplayServicesGetBrightness").map { unsafeBitCast($0, to: Getter.self) }
    }

    static let setBrightness: Setter? = handle.flatMap { handle in
        dlsym(handle, "DisplayServicesSetBrightness").map { unsafeBitCast($0, to: Setter.self) }
    }

    static var isAvailable: Bool { getBrightness != nil && setBrightness != nil }
}

@main
struct SleepToggleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject var appState = AppState()

    var body: some Scene {
        WindowGroup {
            MainView()
                .environmentObject(appState)
                .background(WindowAccessor { window in
                    appDelegate.mainWindow = window
                    appDelegate.appState = appState
                    window.delegate = appDelegate
                    window.isReleasedWhenClosed = false
                    appState.configureInitialWindow(window)
                })
        }
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

// MARK: - WINDOW ACCESSOR
struct WindowAccessor: NSViewRepresentable {
    var callback: (NSWindow) -> Void

    func makeNSView(context: Context) -> NSView {
        WindowObservationView(callback: callback)
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

private final class WindowObservationView: NSView {
    private let callback: (NSWindow) -> Void

    init(callback: @escaping (NSWindow) -> Void) {
        self.callback = callback
        super.init(frame: .zero)
    }

    required init?(coder: NSCoder) { nil }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let window { callback(window) }
    }
}

// MARK: - APP STATE
final class AppState: ObservableObject {
    private static let preferencesApplicationID = "com.ych.sleepdisabler" as CFString
    private static let lidDimmerPreferenceKey = "lidDimmerEnabled" as CFString

    @Published var isFullySleepDisabled = false
    @Published var launchAtLoginEnabled = false
    @Published private(set) var supportsBatteryFailsafe = false
    @Published private(set) var supportsLidDimmer = false

    @Published var menuBarMode = false {
        didSet { UserDefaults.standard.set(menuBarMode, forKey: "menuBarMode") }
    }

    @Published var failsafeEnabled = false {
        didSet {
            UserDefaults.standard.set(failsafeEnabled, forKey: "failsafeEnabled")
            if failsafeEnabled { checkBatteryFailsafe() }
        }
    }

    @Published var failsafeThreshold: Int = 20 {
        didSet {
            UserDefaults.standard.set(failsafeThreshold, forKey: "failsafeThreshold")
            if failsafeEnabled { checkBatteryFailsafe() }
        }
    }

    @Published var lidDimmerEnabled = false {
        didSet {
            if lidDimmerEnabled && !supportsLidDimmer {
                lidDimmerEnabled = false
                return
            }
            Self.saveLidDimmerPreference(lidDimmerEnabled)
            updateLidMonitoring()
            checkLidState()
        }
    }

    @Published var sleepTimerDays = 0 {
        didSet {
            UserDefaults.standard.set(sleepTimerDays, forKey: "sleepTimerDays")
            refreshMenuBar()
        }
    }

    @Published var sleepTimerHours = 0 {
        didSet {
            UserDefaults.standard.set(sleepTimerHours, forKey: "sleepTimerHours")
            refreshMenuBar()
        }
    }

    @Published var sleepTimerMinutes = 30 {
        didSet {
            UserDefaults.standard.set(sleepTimerMinutes, forKey: "sleepTimerMinutes")
            refreshMenuBar()
        }
    }

    @Published var sleepTimerLidClosedOnly = false {
        didSet {
            UserDefaults.standard.set(sleepTimerLidClosedOnly, forKey: "sleepTimerLidClosedOnly")
            updateLidMonitoring()
        }
    }

    @Published var sleepTimerSleepDisabledOnly = false {
        didSet {
            UserDefaults.standard.set(sleepTimerSleepDisabledOnly, forKey: "sleepTimerSleepDisabledOnly")
            if sleepTimerSleepDisabledOnly && sleepTimerRunning && !isFullySleepDisabled {
                sleepTimerRunning = false
            }
        }
    }

    @Published var sleepTimerRunning = false {
        didSet {
            sleepTimerRunning ? startSleepTimer() : stopSleepTimer()
            updateLidMonitoring()
        }
    }

    @Published private(set) var sleepTimerRemaining: TimeInterval = 0

    private var statusItem: NSStatusItem?
    private var refreshTimer: Timer?
    private var isTogglingSleep = false

    // Dimmer state
    private var originalBrightness: Float? = nil
    private var isCurrentlyDimmed = false
    private var lidCheckTimer: Timer?
    private var lidRootDomain: io_service_t = IO_OBJECT_NULL
    private var lidNotificationPort: IONotificationPortRef?
    private var lidInterestNotification: io_object_t = IO_OBJECT_NULL
    private var lidInterestSource: CFRunLoopSource?
    private var workspaceObservers: [NSObjectProtocol] = []
    private var batteryNotificationSource: CFRunLoopSource?
    private var sleepTimer: Timer?
    private var sleepTimerEndDate: Date?
    private weak var sleepSchedulerRemainingMenuItem: NSMenuItem?

    var hasSleepTimerDuration: Bool { sleepTimerDuration > 0 }

    var sleepTimerRemainingText: String {
        let seconds = max(0, Int(sleepTimerRemaining.rounded(.up)))
        let days = seconds / 86_400
        let hours = (seconds % 86_400) / 3_600
        let minutes = (seconds % 3_600) / 60
        let remainingSeconds = seconds % 60
        if days > 0 { return "\(days)d \(hours)h \(minutes)m" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        if minutes > 0 { return "\(minutes)m \(remainingSeconds)s" }
        return "\(remainingSeconds)s"
    }

    private var sleepTimerDuration: TimeInterval {
        TimeInterval(sleepTimerDays * 86_400 + sleepTimerHours * 3_600 + sleepTimerMinutes * 60)
    }

    // Notification Window
    private var notificationWindow: NSWindow?

    init() {
        menuBarMode = UserDefaults.standard.object(forKey: "menuBarMode") as? Bool ?? false
        failsafeEnabled = UserDefaults.standard.object(forKey: "failsafeEnabled") as? Bool ?? false
        let threshold = UserDefaults.standard.integer(forKey: "failsafeThreshold")
        failsafeThreshold = threshold > 0 ? threshold : 20
        let savedLidDimmerEnabled = Self.loadLidDimmerPreference()
        sleepTimerDays = min(max(0, UserDefaults.standard.integer(forKey: "sleepTimerDays")), 365)
        sleepTimerHours = min(max(0, UserDefaults.standard.integer(forKey: "sleepTimerHours")), 23)
        let savedMinutes = UserDefaults.standard.object(forKey: "sleepTimerMinutes") as? Int
        sleepTimerMinutes = min(max(0, savedMinutes ?? 30), 59)
        sleepTimerLidClosedOnly = UserDefaults.standard.object(forKey: "sleepTimerLidClosedOnly") as? Bool ?? false
        sleepTimerSleepDisabledOnly = UserDefaults.standard.object(forKey: "sleepTimerSleepDisabledOnly") as? Bool ?? false
        supportsLidDimmer = Self.isMacBook() && DisplayBrightness.isAvailable
        lidDimmerEnabled = supportsLidDimmer && savedLidDimmerEnabled

        updateActivationPolicy()
        refreshAll()
        installBatteryNotifications()
        installLidNotifications()

        if menuBarMode {
            createMenuBar()
            DispatchQueue.main.async {
                self.hideAllWindows()
            }
        }

        startRefreshTimer()
        updateLidMonitoring()
    }

    private static func loadLidDimmerPreference() -> Bool {
        _ = CFPreferencesSynchronize(
            preferencesApplicationID,
            kCFPreferencesCurrentUser,
            kCFPreferencesAnyHost
        )
        let value = CFPreferencesCopyValue(
            lidDimmerPreferenceKey,
            preferencesApplicationID,
            kCFPreferencesCurrentUser,
            kCFPreferencesAnyHost
        )
        return (value as? NSNumber)?.boolValue ?? false
    }

    private static func saveLidDimmerPreference(_ enabled: Bool) {
        CFPreferencesSetValue(
            lidDimmerPreferenceKey,
            enabled ? kCFBooleanTrue : kCFBooleanFalse,
            preferencesApplicationID,
            kCFPreferencesCurrentUser,
            kCFPreferencesAnyHost
        )
        _ = CFPreferencesSynchronize(
            preferencesApplicationID,
            kCFPreferencesCurrentUser,
            kCFPreferencesAnyHost
        )
    }

    deinit {
        refreshTimer?.invalidate()
        lidCheckTimer?.invalidate()
        sleepTimer?.invalidate()
        if let batteryNotificationSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), batteryNotificationSource, .commonModes)
        }
        workspaceObservers.forEach(NSWorkspace.shared.notificationCenter.removeObserver)
        if lidInterestNotification != IO_OBJECT_NULL { IOObjectRelease(lidInterestNotification) }
        if lidRootDomain != IO_OBJECT_NULL { IOObjectRelease(lidRootDomain) }
        if let lidNotificationPort {
            if let lidInterestSource {
                CFRunLoopRemoveSource(CFRunLoopGetMain(), lidInterestSource, .commonModes)
            }
            IONotificationPortDestroy(lidNotificationPort)
        }
    }

    func configureInitialWindow(_ window: NSWindow) {
        window.setContentSize(NSSize(width: 440, height: 620))
        if menuBarMode {
            window.alphaValue = 0
            window.animationBehavior = .none
            window.orderOut(nil)
        } else {
            window.alphaValue = 1
        }
    }

    // MARK: - TIMERS
    func startRefreshTimer() {
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.refreshSleepState()
            self?.refreshLaunchState()
            self?.checkBatteryFailsafe()
        }
    }

    func updateLidMonitoring() {
        let needsLidState = supportsLidDimmer
            && (lidDimmerEnabled || (sleepTimerRunning && sleepTimerLidClosedOnly))
        if needsLidState {
            checkLidState()
            guard lidCheckTimer == nil else { return }
            lidCheckTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
                self?.checkLidState()
            }
        } else {
            lidCheckTimer?.invalidate()
            lidCheckTimer = nil
            if isCurrentlyDimmed { restoreBrightness() }
        }
    }

    func refreshAll() {
        refreshSleepState()
        refreshLaunchState()
    }

    // MARK: - MODE & WINDOW
    func toggleMode() {
        menuBarMode.toggle()
        updateActivationPolicy()

        if menuBarMode {
            createMenuBar()
            hideAllWindows()
        } else {
            removeMenuBar()
            showWindow()
        }
    }

    func updateActivationPolicy() {
        NSApp.setActivationPolicy(menuBarMode ? .accessory : .regular)
    }

    func showWindow() {
        updateActivationPolicy()
        NSApplication.shared.activate(ignoringOtherApps: true)

        if let window = NSApplication.shared.windows.first(where: { $0.styleMask.contains(.titled) && $0 != notificationWindow }) {
            window.alphaValue = 1
            window.animationBehavior = .default
            if window.isMiniaturized {
                window.deminiaturize(nil)
            }
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
        }
    }

    func hideAllWindows() {
        for window in NSApplication.shared.windows {
            if window != notificationWindow {
                window.orderOut(nil)
            }
        }
        updateActivationPolicy()
    }

    // MARK: - MENU BAR
    func createMenuBar() {
        if statusItem != nil { return }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateMenuBarIcon()

        let menu = NSMenu()

        let toggle = NSMenuItem(
            title: isFullySleepDisabled ? "Enable Sleep" : "Disable Sleep",
            action: #selector(toggleSleepMenu),
            keyEquivalent: ""
        )
        toggle.target = self
        menu.addItem(toggle)
        menu.addItem(.separator())

        let dimmer = NSMenuItem(
            title: lidDimmerEnabled ? "Disable Lid Dimmer" : "Enable Lid Dimmer",
            action: #selector(toggleDimmerMenu),
            keyEquivalent: ""
        )
        dimmer.target = self
        dimmer.isEnabled = supportsLidDimmer
        menu.addItem(dimmer)
        menu.addItem(.separator())

        if sleepTimerRunning {
            let remaining = NSMenuItem(
                title: "Sleep Scheduler: \(sleepTimerRemainingText) remaining",
                action: nil,
                keyEquivalent: ""
            )
            remaining.isEnabled = false
            menu.addItem(remaining)
            sleepSchedulerRemainingMenuItem = remaining
        }

        let timer = NSMenuItem(
            title: sleepTimerRunning ? "Stop Sleep Scheduler" : "Start Last Used Sleep Scheduler (\(savedSleepTimerDescription))",
            action: #selector(toggleSleepTimerMenu),
            keyEquivalent: ""
        )
        timer.target = self
        timer.isEnabled = sleepTimerRunning || hasSleepTimerDuration
        menu.addItem(timer)
        menu.addItem(.separator())

        let show = NSMenuItem(
            title: "Show Window",
            action: #selector(showWindowMenu),
            keyEquivalent: ""
        )
        show.target = self
        menu.addItem(show)

        let mode = NSMenuItem(
            title: menuBarMode ? "Switch to Window Mode" : "Switch to Menu Bar Mode",
            action: #selector(toggleModeMenu),
            keyEquivalent: ""
        )
        mode.target = self
        menu.addItem(mode)
        menu.addItem(.separator())

        let login = NSMenuItem(
            title: launchAtLoginEnabled ? "Disable Login Item" : "Enable Login Item",
            action: #selector(toggleLoginMenu),
            keyEquivalent: ""
        )
        login.target = self
        menu.addItem(login)
        menu.addItem(.separator())

        let quit = NSMenuItem(
            title: "Quit",
            action: #selector(quitMenu),
            keyEquivalent: ""
        )
        quit.target = self
        menu.addItem(quit)

        statusItem?.menu = menu
    }

    func removeMenuBar() {
        if let item = statusItem {
            NSStatusBar.system.removeStatusItem(item)
        }
        statusItem = nil
    }

    func refreshMenuBar() {
        removeMenuBar()
        if menuBarMode {
            createMenuBar()
        }
    }

    func updateMenuBarIcon() {
        statusItem?.button?.image = NSImage(
            systemSymbolName: isFullySleepDisabled ? "lock.open.fill" : "lock.fill",
            accessibilityDescription: nil
        )
    }

    @objc func toggleSleepMenu() { toggleSleep() }
    @objc func showWindowMenu() { showWindow() }
    @objc func toggleModeMenu() { toggleMode() }
    @objc func toggleLoginMenu() { toggleLaunchAtLogin() }
    @objc func toggleDimmerMenu() {
        guard supportsLidDimmer else { return }
        lidDimmerEnabled.toggle()
        refreshMenuBar()
    }
    @objc func toggleSleepTimerMenu() { sleepTimerRunning.toggle() }
    @objc func quitMenu() { NSApp.terminate(nil) }

    // MARK: - SLEEP STATE MANAGEMENT
    func refreshSleepState() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
        process.arguments = ["-g", "custom"]
        let pipe = Pipe()
        process.standardOutput = pipe

        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(decoding: data, as: UTF8.self)

            let isDisabled = output.range(
                of: #"(?m)^\s*(sleep\s+0|disablesleep\s+1)\b"#,
                options: .regularExpression
            ) != nil

            DispatchQueue.main.async {
                guard self.isTogglingSleep == false else { return }
                self.isFullySleepDisabled = isDisabled
                if self.sleepTimerSleepDisabledOnly && self.sleepTimerRunning && !isDisabled {
                    self.sleepTimerRunning = false
                }
                self.updateMenuBarIcon()
                self.refreshMenuBar()
            }
        } catch {
            print("refreshSleepState error: \(error)")
        }
    }

    func saveCurrentPmsetValues() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
        process.arguments = ["-g", "custom"]
        let pipe = Pipe()
        process.standardOutput = pipe

        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(decoding: data, as: UTF8.self)

            var currentSection = ""
            var battSleep = -1, battDisplay = -1
            var acSleep = -1, acDisplay = -1

            for line in output.components(separatedBy: .newlines) {
                let lower = line.lowercased()
                if lower.contains("battery power:") {
                    currentSection = "batt"
                } else if lower.contains("ac power:") {
                    currentSection = "ac"
                }

                let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                let tokens = trimmed.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
                if tokens.count >= 2 {
                    let key = tokens[0].lowercased()
                    if let val = Int(tokens[1]) {
                        if key == "sleep" {
                            if currentSection == "batt" { battSleep = val }
                            else if currentSection == "ac" { acSleep = val }
                        } else if key == "displaysleep" {
                            if currentSection == "batt" { battDisplay = val }
                            else if currentSection == "ac" { acDisplay = val }
                        }
                    }
                }
            }

            print("pmset parsed values -> Batt: sleep=\(battSleep), disp=\(battDisplay) | AC: sleep=\(acSleep), disp=\(acDisplay)")

            if !isFullySleepDisabled {
                if battSleep >= 0 { UserDefaults.standard.set(battSleep, forKey: "origBattSleep") }
                if battDisplay >= 0 { UserDefaults.standard.set(battDisplay, forKey: "origBattDisplay") }
                if acSleep >= 0 { UserDefaults.standard.set(acSleep, forKey: "origAcSleep") }
                if acDisplay >= 0 { UserDefaults.standard.set(acDisplay, forKey: "origAcDisplay") }
            }
        } catch {
            print("Failed to read pmset custom settings: \(error)")
        }
    }

    func toggleSleep() {
        isTogglingSleep = true
        let enabling = !isFullySleepDisabled

        if !enabling && sleepTimerSleepDisabledOnly && sleepTimerRunning {
            sleepTimerRunning = false
        }

        if enabling {
            saveCurrentPmsetValues()
            runCommand(["-a", "disablesleep", "1"])
            runCommand(["-a", "sleep", "0"])
            runCommand(["-a", "displaysleep", "0"])
        } else {
            let bSleep = UserDefaults.standard.object(forKey: "origBattSleep") as? Int ?? 10
            let bDisp = UserDefaults.standard.object(forKey: "origBattDisplay") as? Int ?? 10
            let cSleep = UserDefaults.standard.object(forKey: "origAcSleep") as? Int ?? 10
            let cDisp = UserDefaults.standard.object(forKey: "origAcDisplay") as? Int ?? 10

            print("Restoring settings -> Batt: sleep=\(bSleep), disp=\(bDisp) | AC: sleep=\(cSleep), disp=\(cDisp)")

            runCommand(["-a", "disablesleep", "0"])
            runCommand(["-b", "sleep", "\(bSleep)"])
            runCommand(["-b", "displaysleep", "\(bDisp)"])
            runCommand(["-c", "sleep", "\(cSleep)"])
            runCommand(["-c", "displaysleep", "\(cDisp)"])
        }

        DispatchQueue.main.async {
            self.isFullySleepDisabled = enabling
            self.updateMenuBarIcon()
            self.refreshMenuBar()
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            self.isTogglingSleep = false
            self.refreshSleepState()
        }
    }

    // MARK: - BATTERY FAILSAFE
    func checkBatteryFailsafe() {
        let status = currentBatteryStatus()
        supportsBatteryFailsafe = status.hasInternalBattery
        guard failsafeEnabled, isFullySleepDisabled, status.isDrawingFromBattery,
              let percentage = status.percentage, percentage < failsafeThreshold else { return }
        triggerFailsafe()
    }

    private func installBatteryNotifications() {
        batteryNotificationSource = IOPSNotificationCreateRunLoopSource({ context in
            guard let context else { return }
            let appState = Unmanaged<AppState>.fromOpaque(context).takeUnretainedValue()
            DispatchQueue.main.async { appState.checkBatteryFailsafe() }
        }, Unmanaged.passUnretained(self).toOpaque())?.takeRetainedValue()

        if let batteryNotificationSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), batteryNotificationSource, .commonModes)
        }
        checkBatteryFailsafe()
    }

    private func currentBatteryStatus() -> (hasInternalBattery: Bool, percentage: Int?, isDrawingFromBattery: Bool) {
        guard let snapshot = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let sources = IOPSCopyPowerSourcesList(snapshot)?.takeRetainedValue() as? [CFTypeRef] else {
            return (false, nil, false)
        }

        var percentage: Int?
        var hasInternalBattery = false
        for source in sources {
            guard let description = IOPSGetPowerSourceDescription(snapshot, source)?.takeUnretainedValue() as? [String: Any],
                  description[kIOPSTypeKey as String] as? String == (kIOPSInternalBatteryType as String) else {
                continue
            }
            hasInternalBattery = true
            if let current = description[kIOPSCurrentCapacityKey as String] as? Int,
               let maximum = description[kIOPSMaxCapacityKey as String] as? Int,
               maximum > 0 {
                percentage = min(100, max(0, Int((Double(current) / Double(maximum) * 100).rounded())))
            }
        }

        let provider = IOPSGetProvidingPowerSourceType(snapshot).takeRetainedValue() as String
        return (hasInternalBattery, percentage, provider == (kIOPSBatteryPowerValue as String))
    }

    func triggerFailsafe() {
        DispatchQueue.main.async {
            self.showNotificationWindow()
        }
        forceMacToSleep()
    }

    func showNotificationWindow() {
        if notificationWindow == nil {
            let notifView = NotificationView()
            let hostingController = NSHostingController(rootView: notifView)
            let win = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 320, height: 180),
                styleMask: [.titled, .closable],
                backing: .buffered,
                defer: false
            )
            win.title = "Critical Battery"
            win.contentViewController = hostingController
            win.isReleasedWhenClosed = false
            win.level = .floating
            notificationWindow = win
        }
        notificationWindow?.center()
        notificationWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - LID DIMMER
    func checkLidState() {
        guard supportsLidDimmer,
              lidDimmerEnabled || (sleepTimerRunning && sleepTimerLidClosedOnly) else {
            if isCurrentlyDimmed { restoreBrightness() }
            return
        }

        let isClosed = isLidClosed()

        if sleepTimerRunning && sleepTimerLidClosedOnly && !isClosed {
            sleepTimerRunning = false
        }

        guard lidDimmerEnabled, isFullySleepDisabled else {
            if isCurrentlyDimmed { restoreBrightness() }
            return
        }

        if isClosed && !isCurrentlyDimmed {
            dimBrightness()
        } else if !isClosed && isCurrentlyDimmed {
            restoreBrightness()
        }
    }

    private func isLidClosed() -> Bool {
        guard lidRootDomain != IO_OBJECT_NULL,
              let value = IORegistryEntryCreateCFProperty(
                lidRootDomain,
                "AppleClamshellState" as CFString,
                kCFAllocatorDefault,
                0
              )?.takeRetainedValue() else { return false }
        if let bool = value as? Bool { return bool }
        return (value as? NSNumber)?.boolValue ?? false
    }

    private func installLidNotifications() {
        lidRootDomain = IOServiceGetMatchingService(
            kIOMainPortDefault,
            IOServiceMatching("IOPMrootDomain")
        )
        guard lidRootDomain != IO_OBJECT_NULL else { return }

        let port = IONotificationPortCreate(kIOMainPortDefault)
        var notification: io_object_t = IO_OBJECT_NULL
        let result = IOServiceAddInterestNotification(
            port,
            lidRootDomain,
            kIOGeneralInterest,
            { context, _, _, _ in
                guard let context else { return }
                let appState = Unmanaged<AppState>.fromOpaque(context).takeUnretainedValue()
                DispatchQueue.main.async { appState.checkLidState() }
            },
            Unmanaged.passUnretained(self).toOpaque(),
            &notification
        )

        if result == KERN_SUCCESS {
            lidNotificationPort = port
            lidInterestNotification = notification
            lidInterestSource = IONotificationPortGetRunLoopSource(port).takeUnretainedValue()
            if let lidInterestSource {
                CFRunLoopAddSource(CFRunLoopGetMain(), lidInterestSource, .commonModes)
            }
        } else {
            IONotificationPortDestroy(port)
        }

        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didWakeNotification, NSWorkspace.willSleepNotification] {
            workspaceObservers.append(
                center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                    self?.checkLidState()
                }
            )
        }
    }

    private static func isMacBook() -> Bool {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/system_profiler")
        process.arguments = ["SPHardwareDataType", "-json"]
        process.standardOutput = output
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0,
                  let report = try JSONSerialization.jsonObject(
                    with: output.fileHandleForReading.readDataToEndOfFile()
                  ) as? [String: Any],
                  let hardware = report["SPHardwareDataType"] as? [[String: Any]],
                  let modelName = hardware.first?["machine_name"] as? String else { return false }
            return modelName.range(of: "MacBook", options: .caseInsensitive) != nil
        } catch {
            return false
        }
    }

    func dimBrightness() {
        guard supportsLidDimmer, let getBrightness = DisplayBrightness.getBrightness,
              let setBrightness = DisplayBrightness.setBrightness else { return }
        let display = CGMainDisplayID()
        var current: Float = 0
        guard getBrightness(display, &current) == 0 else { return }
        guard setBrightness(display, 0.0) == 0 else { return }
        originalBrightness = current
        isCurrentlyDimmed = true
    }

    func restoreBrightness() {
        if let orig = originalBrightness, let setBrightness = DisplayBrightness.setBrightness {
            _ = setBrightness(CGMainDisplayID(), orig)
        }
        originalBrightness = nil
        isCurrentlyDimmed = false
    }

    // MARK: - SLEEP TIMER
    func startSleepTimer() {
        guard hasSleepTimerDuration else {
            sleepTimerRunning = false
            return
        }
        guard !sleepTimerLidClosedOnly || isLidClosed() else {
            sleepTimerRunning = false
            return
        }
        guard !sleepTimerSleepDisabledOnly || isFullySleepDisabled else {
            sleepTimerRunning = false
            return
        }

        stopSleepTimer()
        sleepTimerEndDate = Date().addingTimeInterval(sleepTimerDuration)
        updateSleepTimerRemaining()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            self?.updateSleepTimerRemaining()
        }
        RunLoop.main.add(timer, forMode: .common)
        sleepTimer = timer
        refreshMenuBar()
    }

    func stopSleepTimer() {
        sleepTimer?.invalidate()
        sleepTimer = nil
        sleepTimerEndDate = nil
        sleepTimerRemaining = 0
        refreshMenuBar()
    }

    private func updateSleepTimerRemaining() {
        guard let endDate = sleepTimerEndDate else { return }
        let remaining = endDate.timeIntervalSinceNow
        if remaining <= 0 {
            sleepTimerRunning = false
            forceMacToSleep()
        } else {
            sleepTimerRemaining = remaining
            sleepSchedulerRemainingMenuItem?.title = "Sleep Scheduler: \(sleepTimerRemainingText) remaining"
        }
    }

    private var savedSleepTimerDescription: String {
        let components = [
            sleepTimerDays > 0 ? "\(sleepTimerDays)d" : nil,
            sleepTimerHours > 0 ? "\(sleepTimerHours)h" : nil,
            sleepTimerMinutes > 0 ? "\(sleepTimerMinutes)m" : nil
        ].compactMap { $0 }
        return components.joined(separator: " ")
    }

    private func forceMacToSleep() {
        if isCurrentlyDimmed { restoreBrightness() }
        let needsRestore = isFullySleepDisabled
        if needsRestore { toggleSleep() }

        DispatchQueue.main.asyncAfter(deadline: .now() + (needsRestore ? 1.5 : 0)) {
            self.runCommand(["sleepnow"])
        }
    }

    // MARK: - LAUNCH AT LOGIN
    func refreshLaunchState() {
        launchAtLoginEnabled = SMAppService.mainApp.status == .enabled
    }

    func toggleLaunchAtLogin() {
        do {
            if launchAtLoginEnabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
            refreshLaunchState()
            refreshMenuBar()
        } catch {
            print("toggleLaunchAtLogin error: \(error)")
        }
    }

    // MARK: - COMMAND EXECUTION
    func runCommand(_ args: [String]) {
        guard isAllowedPmsetCommand(args) else {
            print("Blocked unexpected pmset arguments: \(args)")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/sudo")
        process.arguments = ["-n", "/usr/bin/pmset"] + args
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            print("runCommand error: \(error)")
        }
    }

    private func isAllowedPmsetCommand(_ args: [String]) -> Bool {
        let fixedCommands = [
            ["sleepnow"],
            ["-a", "disablesleep", "0"],
            ["-a", "disablesleep", "1"],
            ["-a", "sleep", "0"],
            ["-a", "displaysleep", "0"]
        ]
        if fixedCommands.contains(args) { return true }
        guard args.count == 3,
              ["-b", "-c"].contains(args[0]),
              ["sleep", "displaysleep"].contains(args[1]),
              let value = Int(args[2]) else { return false }
        return (0...180).contains(value)
    }
}

// MARK: - APP DELEGATE
private enum PmsetPermissionStatus {
    case restricted
    case legacyBroad
    case missing
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    weak var mainWindow: NSWindow?
    weak var appState: AppState?

    func applicationWillFinishLaunching(_ notification: Notification) {
        let startsInMenuBarMode = UserDefaults.standard.object(forKey: "menuBarMode") as? Bool ?? false
        NSApp.setActivationPolicy(startsInMenuBarMode ? .accessory : .regular)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.async {
            self.checkPmsetPermission()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        appState?.restoreBrightness()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        appState?.updateActivationPolicy()
        appState?.showWindow()
        return false // Prevents macOS / SwiftUI from creating duplicate windows
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        appState?.updateActivationPolicy()
        return false // Prevents SwiftUI from destroying the window!
    }

    func checkPmsetPermission() {
        let status = pmsetPermissionStatus()
        guard status != .restricted else { return }

        let explanation = NSAlert()
        explanation.messageText = status == .legacyBroad
            ? "Update Sleep Control Permission"
            : "Install Sleep Control Permission"
        explanation.informativeText = status == .legacyBroad
            ? "Sleep Disabler found its older unrestricted pmset rule. It needs one administrator approval to replace that file with a safer rule limited to the exact sleep commands used by this app. Your password is handled by macOS and is never seen or stored by Sleep Disabler."
            : "Sleep Disabler needs one administrator approval to install a restricted permission file in /etc/sudoers.d. It permits only the exact sleep commands used by this app. Your password is handled by macOS and is never seen or stored by Sleep Disabler."
        explanation.addButton(withTitle: status == .legacyBroad ? "Update Permission" : "Install Permission")
        explanation.addButton(withTitle: "Not Now")
        guard explanation.runModal() == .alertFirstButtonReturn else { return }

        let installation = installRestrictedPmsetPermission()
        guard installation.succeeded, pmsetPermissionStatus() == .restricted else {
            let alert = NSAlert()
            alert.messageText = "Sleep Control Permission Was Not Installed"
            alert.informativeText = installation.errorMessage
                ?? "The installer completed, but the restricted permission could not be verified. The existing permission file was left unchanged."
            alert.runModal()
            return
        }
    }

    private func pmsetPermissionStatus() -> PmsetPermissionStatus {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/sudo")
        process.arguments = ["-n", "-ll"]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return .missing }
            let listing = String(decoding: data, as: UTF8.self)
            let entries = listing.components(separatedBy: "Sudoers entry:")
            let noPasswordEntries = entries.filter { $0.contains("Options: !authenticate") }
            let legacyPattern = #"(?m)^\s*/usr/bin/pmset\s*$"#
            if noPasswordEntries.contains(where: {
                $0.range(of: legacyPattern, options: .regularExpression) != nil
            }) { return .legacyBroad }

            let requiredRuleFragments = [
                "/usr/bin/pmset ^-a disablesleep [01]$",
                "/usr/bin/pmset ^-a sleep 0$",
                "/usr/bin/pmset ^-a displaysleep 0$",
                "/usr/bin/pmset ^-[bc] (sleep|displaysleep) (0|[1-9][0-9]?|1[0-7][0-9]|180)$",
                "/usr/bin/pmset ^sleepnow$"
            ]
            let hasRestrictedRules = requiredRuleFragments.allSatisfy { fragment in
                noPasswordEntries.contains(where: { $0.contains(fragment) })
            }
            return hasRestrictedRules ? .restricted : .missing
        } catch {
            return .missing
        }
    }

    private func installRestrictedPmsetPermission() -> (succeeded: Bool, errorMessage: String?) {
        let temporaryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("sleep_disabler_sudoers_\(UUID().uuidString)")
        let rule = """
        # Sleep Disabler restricted pmset policy v2
        # Generated by Sleep Disabler; do not edit this file.
        %admin ALL=(root) NOPASSWD: /usr/bin/pmset ^-a disablesleep [01]$, /usr/bin/pmset ^-a sleep 0$, /usr/bin/pmset ^-a displaysleep 0$, /usr/bin/pmset ^-[bc] (sleep|displaysleep) (0|[1-9][0-9]?|1[0-7][0-9]|180)$, /usr/bin/pmset ^sleepnow$
        """
        do {
            try rule.write(to: temporaryURL, atomically: true, encoding: .utf8)
        } catch {
            return (false, "Sleep Disabler could not create its temporary permission file: \(error.localizedDescription)")
        }
        defer { try? FileManager.default.removeItem(at: temporaryURL) }

        let source = shellQuoted(temporaryURL.path)
        let destination = shellQuoted("/etc/sudoers.d/sleep_disabler")
        let staging = shellQuoted("/etc/sudoers.d/sleep_disabler.new")
        let recognizedFile = "(/usr/bin/grep -Fqx '# Sleep Disabler restricted pmset policy v2' \(destination) || /usr/bin/grep -Fqx '%admin ALL=(ALL) NOPASSWD: /usr/bin/pmset' \(destination))"
        let protectUnknownFile = "if [ -e \(destination) ]; then \(recognizedFile) || { /bin/echo 'The existing sleep_disabler permission file is not recognized and was not overwritten.' >&2; exit 42; }; fi"
        let command = "\(protectUnknownFile) && /bin/mkdir -p /etc/sudoers.d && /usr/sbin/visudo -cf \(source) && /usr/bin/install -o root -g wheel -m 440 \(source) \(staging) && /usr/sbin/visudo -cf \(staging) && /bin/mv -f \(staging) \(destination)"
        let script = "do shell script \(appleScriptQuoted(command)) with administrator privileges"
        var error: NSDictionary?
        let result = NSAppleScript(source: script)?.executeAndReturnError(&error)
        guard result != nil, error == nil else {
            let message = error?["NSAppleScriptErrorMessage"] as? String
                ?? error?["NSAppleScriptErrorBriefMessage"] as? String
                ?? "macOS did not complete the administrator-authorized installation."
            let number = error?["NSAppleScriptErrorNumber"] as? Int
            let detail = number.map { "\(message) (error \($0))" } ?? message
            return (false, detail)
        }
        return (true, nil)
    }

    private func shellQuoted(_ value: String) -> String {
        "'\(value.replacingOccurrences(of: "'", with: "'\\\"'\\\"'"))'"
    }

    private func appleScriptQuoted(_ value: String) -> String {
        "\"\(value.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\""))\""
    }
}
