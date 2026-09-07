//
//  Sleep_DisablerApp.swift
//  Sleep Disabler
//

import SwiftUI
import AppKit
import ServiceManagement
import Combine

// C-Bridging for DisplayServices (Apple Silicon)
@_silgen_name("DisplayServicesSetBrightness")
func DisplayServicesSetBrightness(_ display: CGDirectDisplayID, _ brightness: Float) -> Int

@_silgen_name("DisplayServicesGetBrightness")
func DisplayServicesGetBrightness(_ display: CGDirectDisplayID, _ brightness: UnsafeMutablePointer<Float>) -> Int

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
        let view = NSView()
        DispatchQueue.main.async {
            if let window = view.window {
                callback(window)
            }
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

// MARK: - APP STATE
final class AppState: ObservableObject {
    @Published var isFullySleepDisabled = false
    @Published var launchAtLoginEnabled = false

    @Published var menuBarMode = false {
        didSet { UserDefaults.standard.set(menuBarMode, forKey: "menuBarMode") }
    }

    @Published var failsafeEnabled = false {
        didSet { UserDefaults.standard.set(failsafeEnabled, forKey: "failsafeEnabled") }
    }

    @Published var failsafeThreshold: Int = 20 {
        didSet { UserDefaults.standard.set(failsafeThreshold, forKey: "failsafeThreshold") }
    }

    @Published var lidDimmerEnabled = false {
        didSet { UserDefaults.standard.set(lidDimmerEnabled, forKey: "lidDimmerEnabled") }
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
        didSet { UserDefaults.standard.set(sleepTimerLidClosedOnly, forKey: "sleepTimerLidClosedOnly") }
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
        didSet { sleepTimerRunning ? startSleepTimer() : stopSleepTimer() }
    }

    @Published private(set) var sleepTimerRemaining: TimeInterval = 0

    private var statusItem: NSStatusItem?
    private var refreshTimer: Timer?
    private var isTogglingSleep = false

    // Dimmer state
    private var originalBrightness: Float? = nil
    private var isCurrentlyDimmed = false
    private var lidCheckTimer: Timer?
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
        lidDimmerEnabled = UserDefaults.standard.object(forKey: "lidDimmerEnabled") as? Bool ?? false
        sleepTimerDays = min(max(0, UserDefaults.standard.integer(forKey: "sleepTimerDays")), 365)
        sleepTimerHours = min(max(0, UserDefaults.standard.integer(forKey: "sleepTimerHours")), 23)
        let savedMinutes = UserDefaults.standard.object(forKey: "sleepTimerMinutes") as? Int
        sleepTimerMinutes = min(max(0, savedMinutes ?? 30), 59)
        sleepTimerLidClosedOnly = UserDefaults.standard.object(forKey: "sleepTimerLidClosedOnly") as? Bool ?? false
        sleepTimerSleepDisabledOnly = UserDefaults.standard.object(forKey: "sleepTimerSleepDisabledOnly") as? Bool ?? false

        updateActivationPolicy()
        refreshAll()

        if menuBarMode {
            createMenuBar()
            DispatchQueue.main.async {
                self.hideAllWindows()
            }
        }

        startRefreshTimer()
        startLidCheckTimer()
    }

    func configureInitialWindow(_ window: NSWindow) {
        window.setContentSize(NSSize(width: 440, height: 620))
        if menuBarMode {
            window.orderOut(nil)
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

    func startLidCheckTimer() {
        lidCheckTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.checkLidState()
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
        guard failsafeEnabled, isFullySleepDisabled else { return }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
        process.arguments = ["-g", "batt"]
        let pipe = Pipe()
        process.standardOutput = pipe

        do {
            try process.run()
            process.waitUntilExit()
            let output = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)

            let isDischarging = output.contains("Now drawing from 'Battery Power'")
            if isDischarging, let match = output.range(of: #"(\d+)%"#, options: .regularExpression) {
                let percStr = output[match].dropLast()
                if let perc = Int(percStr), perc < failsafeThreshold {
                    triggerFailsafe()
                }
            }
        } catch {}
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
        guard lidDimmerEnabled || (sleepTimerRunning && sleepTimerLidClosedOnly) else {
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
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/ioreg")
        p.arguments = ["-r", "-k", "AppleClamshellState", "-d", "4"]
        let pipe = Pipe()
        p.standardOutput = pipe
        try? p.run()
        p.waitUntilExit()
        let out = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        return out.contains("\"AppleClamshellState\" = Yes")
    }

    func dimBrightness() {
        let display = CGMainDisplayID()
        var current: Float = 0
        _ = DisplayServicesGetBrightness(display, &current)
        originalBrightness = current
        _ = DisplayServicesSetBrightness(display, 0.0)
        isCurrentlyDimmed = true
    }

    func restoreBrightness() {
        if let orig = originalBrightness {
            _ = DisplayServicesSetBrightness(CGMainDisplayID(), orig)
        }
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
}

// MARK: - APP DELEGATE
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    weak var mainWindow: NSWindow?
    weak var appState: AppState?

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.async {
            self.checkPmsetPermission()
        }
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
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/sudo")
        process.arguments = ["-n", "/usr/bin/pmset", "-g"]
        do {
            try process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 { return }
        } catch { }
        promptForSudoersInstall()
    }

    func promptForSudoersInstall() {
        let alert = NSAlert()
        alert.messageText = "Permission Required"
        alert.informativeText = "Install pmset permission rule?"
        alert.addButton(withTitle: "Enable")
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn {
            installSudoers()
        }
    }

    func installSudoers() {
        let temp = "/tmp/sleep_disabler"
        let content = "%admin ALL=(ALL) NOPASSWD: /usr/bin/pmset\n"
        try? content.write(toFile: temp, atomically: true, encoding: .utf8)
        let cmd = "sudo mkdir -p /etc/sudoers.d && sudo cp \(temp) /etc/sudoers.d/sleep_disabler && sudo chmod 440 /etc/sudoers.d/sleep_disabler"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-c", cmd]
        try? p.run()
        p.waitUntilExit()
        try? FileManager.default.removeItem(atPath: temp)
    }
}
