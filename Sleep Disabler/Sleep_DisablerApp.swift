//
//  Sleep_DisablerApp.swift
//  Sleep Disabler
//

import AppKit
import Combine
import ServiceManagement
import SwiftUI

@main
struct SleepToggleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState = AppState()

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
        .commands { CommandGroup(replacing: .newItem) {} }
    }
}

struct WindowAccessor: NSViewRepresentable {
    let callback: (NSWindow) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            if let window = view.window { callback(window) }
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

final class AppState: ObservableObject {
    @Published private(set) var isFullySleepDisabled = false
    @Published private(set) var launchAtLoginEnabled = false
    @Published private(set) var privilegePolicyStatus: PrivilegePolicyStatus = .missingOrInvalid
    @Published private(set) var capabilities = HardwareCapabilities(hasInternalBattery: false, hasBuiltInDisplay: false)

    @Published var menuBarMode = false { didSet { UserDefaults.standard.set(menuBarMode, forKey: "menuBarMode"); applyMode() } }
    @Published var failsafeEnabled = false {
        didSet {
            UserDefaults.standard.set(failsafeEnabled, forKey: "failsafeEnabled")
            checkBatteryFailsafe()
        }
    }
    @Published var failsafeThreshold = 20 {
        didSet {
            UserDefaults.standard.set(failsafeThreshold, forKey: "failsafeThreshold")
            checkBatteryFailsafe()
        }
    }
    @Published var lidDimmerEnabled = false {
        didSet {
            if lidDimmerEnabled && !capabilities.supportsBrightnessDimming { lidDimmerEnabled = false; return }
            UserDefaults.standard.set(lidDimmerEnabled, forKey: "lidDimmerEnabled")
            updateLidMonitoring()
            if !lidDimmerEnabled { brightnessManager.restore() }
            refreshMenuBar()
        }
    }
    @Published var sleepTimerDays = 0 { didSet { saveTimerValue("sleepTimerDays", sleepTimerDays); refreshMenuBar() } }
    @Published var sleepTimerHours = 0 { didSet { saveTimerValue("sleepTimerHours", sleepTimerHours); refreshMenuBar() } }
    @Published var sleepTimerMinutes = 30 { didSet { saveTimerValue("sleepTimerMinutes", sleepTimerMinutes); refreshMenuBar() } }
    @Published var sleepTimerLidClosedOnly = false { didSet { UserDefaults.standard.set(sleepTimerLidClosedOnly, forKey: "sleepTimerLidClosedOnly"); updateLidMonitoring() } }
    @Published var sleepTimerSleepDisabledOnly = false { didSet { UserDefaults.standard.set(sleepTimerSleepDisabledOnly, forKey: "sleepTimerSleepDisabledOnly"); stopSchedulerIfNoLongerEligible() } }
    @Published var sleepTimerRunning = false {
        didSet {
            if sleepTimerRunning { startScheduler() }
            else {
                scheduler.stop()
                updateLidMonitoring()
                refreshMenuBar()
            }
        }
    }
    @Published private(set) var sleepTimerRemaining: TimeInterval = 0

    private let powerManager: PowerManager
    private let batteryMonitor: BatteryMonitor
    private let lidMonitor: LidMonitor
    private let brightnessManager = BrightnessManager()
    private let scheduler = Scheduler()
    private let policyManager: PrivilegePolicyManager
    private let menuBarController = MenuBarController()
    private var batteryStatus = BatteryStatus(hasInternalBattery: false, percentage: nil, isDrawingFromBattery: false)
    private var notificationWindow: NSWindow?
    private var ownsSleepDisablement = false

    var hasSleepTimerDuration: Bool { sleepTimerDuration > 0 }
    var supportsLidDimmer: Bool { capabilities.supportsBrightnessDimming && brightnessManager.isAvailable }
    var supportsBatteryFailsafe: Bool { capabilities.hasInternalBattery }
    var sleepTimerRemainingText: String { Self.durationText(sleepTimerRemaining, includeSeconds: true) }

    private var sleepTimerDuration: TimeInterval {
        TimeInterval(sleepTimerDays * 86_400 + sleepTimerHours * 3_600 + sleepTimerMinutes * 60)
    }

    init(
        powerManager: PowerManager = PowerManager(),
        batteryMonitor: BatteryMonitor = BatteryMonitor(),
        lidMonitor: LidMonitor = LidMonitor(),
        policyManager: PrivilegePolicyManager = PrivilegePolicyManager()
    ) {
        self.powerManager = powerManager
        self.batteryMonitor = batteryMonitor
        self.lidMonitor = lidMonitor
        self.policyManager = policyManager

        menuBarMode = UserDefaults.standard.object(forKey: "menuBarMode") as? Bool ?? false
        failsafeEnabled = UserDefaults.standard.object(forKey: "failsafeEnabled") as? Bool ?? false
        failsafeThreshold = min(max(UserDefaults.standard.object(forKey: "failsafeThreshold") as? Int ?? 20, 1), 99)
        lidDimmerEnabled = UserDefaults.standard.object(forKey: "lidDimmerEnabled") as? Bool ?? false
        sleepTimerDays = min(max(UserDefaults.standard.integer(forKey: "sleepTimerDays"), 0), 365)
        sleepTimerHours = min(max(UserDefaults.standard.integer(forKey: "sleepTimerHours"), 0), 23)
        sleepTimerMinutes = min(max(UserDefaults.standard.object(forKey: "sleepTimerMinutes") as? Int ?? 30, 0), 59)
        sleepTimerLidClosedOnly = UserDefaults.standard.object(forKey: "sleepTimerLidClosedOnly") as? Bool ?? false
        sleepTimerSleepDisabledOnly = UserDefaults.standard.object(forKey: "sleepTimerSleepDisabledOnly") as? Bool ?? false
        ownsSleepDisablement = UserDefaults.standard.bool(forKey: "sleepDisablerOwnsSleepDisablement")

        batteryMonitor.didChange = { [weak self] status in self?.handleBattery(status) }
        lidMonitor.didChange = { [weak self] isClosed in self?.handleLidChange(isClosed) }
        scheduler.didTick = { [weak self] remaining in self?.sleepTimerRemaining = remaining; self?.refreshMenuBar() }
        scheduler.didFinish = { [weak self] in self?.sleepTimerRunning = false; self?.forceMacToSleep() }
        configureMenuBarActions()
        refreshAll()
        updateLidMonitoring()
        applyMode()
    }

    func configureInitialWindow(_ window: NSWindow) {
        window.setContentSize(NSSize(width: 440, height: 620))
        if menuBarMode { window.orderOut(nil) }
    }

    func refreshAll() {
        refreshSleepState()
        refreshLaunchState()
        refreshPrivilegePolicy()
        batteryMonitor.refresh()
    }

    func refreshPrivilegePolicy() { privilegePolicyStatus = policyManager.status() }

    func installOrRepairPrivilegePolicy() -> Bool {
        let installed = policyManager.installOrRepair()
        refreshPrivilegePolicy()
        return installed && privilegePolicyStatus == .restricted
    }

    func refreshSleepState() {
        guard let disabled = powerManager.isSleepDisabled() else { return }
        isFullySleepDisabled = disabled
        stopSchedulerIfNoLongerEligible()
        refreshMenuBar()
    }

    func toggleSleep() {
        if isFullySleepDisabled {
            guard ownsSleepDisablement, let settings = powerManager.savedSettings() else {
                print("Sleep disabling was not created by this app, so its settings will not be changed.")
                return
            }
            guard powerManager.restoreSleep(using: settings) else { return }
            isFullySleepDisabled = false
            setSleepDisablementOwnership(false)
            brightnessManager.restore()
        } else {
            guard let settings = powerManager.saveCurrentSettingsIfNeeded(isSleepDisabled: false) else {
                print("Sleep settings could not be read; refusing to change them without a restoration baseline.")
                return
            }
            guard powerManager.disableSleep() else {
                _ = powerManager.restoreSleep(using: settings)
                return
            }
            isFullySleepDisabled = true
            setSleepDisablementOwnership(true)
        }
        stopSchedulerIfNoLongerEligible()
        refreshMenuBar()
    }

    func toggleMode() { menuBarMode.toggle() }

    func showWindow() {
        NSApp.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
        if let window = NSApplication.shared.windows.first(where: { $0 != notificationWindow && $0.styleMask.contains(.titled) }) {
            if window.isMiniaturized { window.deminiaturize(nil) }
            window.makeKeyAndOrderFront(nil)
        }
    }

    func hideAllWindows() {
        NSApplication.shared.windows.filter { $0 != notificationWindow }.forEach { $0.orderOut(nil) }
    }

    func refreshLaunchState() { launchAtLoginEnabled = SMAppService.mainApp.status == .enabled }

    func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            print("Launch at login update failed: \(error)")
        }
        refreshLaunchState()
        refreshMenuBar()
    }

    func prepareForTermination() {
        scheduler.stop()
        brightnessManager.restore()
        if isFullySleepDisabled, ownsSleepDisablement,
           let settings = powerManager.savedSettings(), powerManager.restoreSleep(using: settings) {
            isFullySleepDisabled = false
            setSleepDisablementOwnership(false)
        }
    }

    private func handleBattery(_ status: BatteryStatus) {
        batteryStatus = status
        let updatedCapabilities = HardwareCapabilities.detect(hasInternalBattery: status.hasInternalBattery)
        capabilities = HardwareCapabilities(
            hasInternalBattery: status.hasInternalBattery,
            hasBuiltInDisplay: capabilities.hasBuiltInDisplay || updatedCapabilities.hasBuiltInDisplay
        )
        if !capabilities.supportsBrightnessDimming && lidDimmerEnabled { lidDimmerEnabled = false }
        checkBatteryFailsafe()
    }

    private func checkBatteryFailsafe() {
        guard failsafeEnabled, isFullySleepDisabled, batteryStatus.hasInternalBattery,
              batteryStatus.isDrawingFromBattery, let percentage = batteryStatus.percentage,
              percentage < failsafeThreshold else { return }
        showNotificationWindow()
        forceMacToSleep()
    }

    private func handleLidChange(_ isClosed: Bool) {
        if sleepTimerRunning && sleepTimerLidClosedOnly && !isClosed { sleepTimerRunning = false }
        guard lidDimmerEnabled, isFullySleepDisabled, supportsLidDimmer else {
            brightnessManager.restore()
            return
        }
        if isClosed { brightnessManager.dimIfPossible(capabilities: capabilities) } else { brightnessManager.restore() }
    }

    private func updateLidMonitoring() {
        lidMonitor.setMonitoringActive(capabilities.supportsLidState && ((lidDimmerEnabled && supportsLidDimmer) || (sleepTimerRunning && sleepTimerLidClosedOnly)))
    }

    private func startScheduler() {
        guard hasSleepTimerDuration,
              (!sleepTimerLidClosedOnly || lidMonitor.currentLidClosed()),
              (!sleepTimerSleepDisabledOnly || isFullySleepDisabled) else {
            if sleepTimerRunning { sleepTimerRunning = false }
            return
        }
        scheduler.start(duration: sleepTimerDuration)
        updateLidMonitoring()
        refreshMenuBar()
    }

    private func stopSchedulerIfNoLongerEligible() {
        if sleepTimerRunning && sleepTimerSleepDisabledOnly && !isFullySleepDisabled { sleepTimerRunning = false }
    }

    private func forceMacToSleep() {
        brightnessManager.restore()
        if isFullySleepDisabled, ownsSleepDisablement {
            guard let settings = powerManager.savedSettings(), powerManager.restoreSleep(using: settings) else { return }
            isFullySleepDisabled = false
            setSleepDisablementOwnership(false)
        }
        _ = powerManager.sleepNow()
        refreshMenuBar()
    }

    private func showNotificationWindow() {
        if notificationWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 320, height: 180), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Critical Battery"
            window.contentViewController = NSHostingController(rootView: NotificationView())
            window.isReleasedWhenClosed = false
            window.level = .floating
            notificationWindow = window
        }
        notificationWindow?.center()
        notificationWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func applyMode() {
        NSApp.setActivationPolicy(menuBarMode ? .accessory : .regular)
        refreshMenuBar()
        if menuBarMode { hideAllWindows() } else { menuBarController.remove() }
    }

    private func configureMenuBarActions() {
        menuBarController.onToggleSleep = { [weak self] in self?.toggleSleep() }
        menuBarController.onToggleDimmer = { [weak self] in self?.lidDimmerEnabled.toggle() }
        menuBarController.onToggleScheduler = { [weak self] in self?.sleepTimerRunning.toggle() }
        menuBarController.onShowWindow = { [weak self] in self?.showWindow() }
        menuBarController.onToggleMode = { [weak self] in self?.toggleMode() }
        menuBarController.onToggleLogin = { [weak self] in guard let self else { return }; self.setLaunchAtLogin(!self.launchAtLoginEnabled) }
        menuBarController.onQuit = { NSApp.terminate(nil) }
    }

    private func refreshMenuBar() {
        menuBarController.update(MenuBarPresentation(
            sleepDisabled: isFullySleepDisabled,
            lidDimmerEnabled: lidDimmerEnabled,
            lidDimmerAvailable: supportsLidDimmer,
            schedulerRunning: sleepTimerRunning,
            schedulerText: sleepTimerRemainingText,
            schedulerCanStart: hasSleepTimerDuration,
            savedDurationText: Self.durationText(sleepTimerDuration, includeSeconds: false),
            menuBarMode: menuBarMode,
            launchAtLoginEnabled: launchAtLoginEnabled
        ))
    }

    private func saveTimerValue(_ key: String, _ value: Int) { UserDefaults.standard.set(value, forKey: key) }

    private func setSleepDisablementOwnership(_ owns: Bool) {
        ownsSleepDisablement = owns
        UserDefaults.standard.set(owns, forKey: "sleepDisablerOwnsSleepDisablement")
    }

    private static func durationText(_ duration: TimeInterval, includeSeconds: Bool) -> String {
        let seconds = max(0, Int(duration.rounded(.up)))
        let days = seconds / 86_400
        let hours = (seconds % 86_400) / 3_600
        let minutes = (seconds % 3_600) / 60
        let remainingSeconds = seconds % 60
        if days > 0 { return "\(days)d \(hours)h \(minutes)m" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        if minutes > 0 { return includeSeconds ? "\(minutes)m \(remainingSeconds)s" : "\(minutes)m" }
        return includeSeconds ? "\(remainingSeconds)s" : "0m"
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    weak var mainWindow: NSWindow?
    weak var appState: AppState?

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.async { self.checkPrivilegePolicy() }
    }

    func applicationWillTerminate(_ notification: Notification) { appState?.prepareForTermination() }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        appState?.showWindow()
        return false
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    private func checkPrivilegePolicy() {
        guard let appState, appState.privilegePolicyStatus != .restricted else { return }
        let alert = NSAlert()
        alert.messageText = appState.privilegePolicyStatus == .legacyBroadRule ? "Update Sleep Disabler Permission" : "Repair Sleep Disabler Permission"
        alert.informativeText = appState.privilegePolicyStatus == .legacyBroadRule
            ? "Sleep Disabler found its older broad pmset permission. Update it to the new restricted policy?"
            : "Install or repair the restricted permission needed for Sleep Disabler's exact power-management commands?"
        alert.addButton(withTitle: appState.privilegePolicyStatus == .legacyBroadRule ? "Update" : "Repair")
        alert.addButton(withTitle: "Not Now")
        if alert.runModal() == .alertFirstButtonReturn, !appState.installOrRepairPrivilegePolicy() {
            let failure = NSAlert()
            failure.messageText = "Permission Was Not Installed"
            failure.informativeText = "Sleep Disabler could not validate or install its restricted policy. No power settings were changed."
            failure.runModal()
        }
    }
}
