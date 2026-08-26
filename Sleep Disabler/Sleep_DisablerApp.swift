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

    private var statusItem: NSStatusItem?
    private var refreshTimer: Timer?
    private var isTogglingSleep = false

    // Dimmer state
    private var originalBrightness: Float? = nil
    private var isCurrentlyDimmed = false
    private var lidCheckTimer: Timer?

    // Notification Window
    private var notificationWindow: NSWindow?

    init() {
        menuBarMode = UserDefaults.standard.object(forKey: "menuBarMode") as? Bool ?? false
        failsafeEnabled = UserDefaults.standard.object(forKey: "failsafeEnabled") as? Bool ?? false
        let threshold = UserDefaults.standard.integer(forKey: "failsafeThreshold")
        failsafeThreshold = threshold > 0 ? threshold : 20
        lidDimmerEnabled = UserDefaults.standard.object(forKey: "lidDimmerEnabled") as? Bool ?? false

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
        window.setContentSize(NSSize(width: 440, height: 480))
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
        if isCurrentlyDimmed { restoreBrightness() }
        if isFullySleepDisabled { toggleSleep() }

        DispatchQueue.main.async {
            self.showNotificationWindow()
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
            p.arguments = ["sleepnow"]
            try? p.run()
        }
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
        guard lidDimmerEnabled, isFullySleepDisabled else {
            if isCurrentlyDimmed { restoreBrightness() }
            return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/ioreg")
        p.arguments = ["-r", "-k", "AppleClamshellState", "-d", "4"]
        let pipe = Pipe()
        p.standardOutput = pipe
        try? p.run()
        p.waitUntilExit()
        let out = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)

        let isClosed = out.contains("\"AppleClamshellState\" = Yes")

        if isClosed && !isCurrentlyDimmed {
            dimBrightness()
        } else if !isClosed && isCurrentlyDimmed {
            restoreBrightness()
        }
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
