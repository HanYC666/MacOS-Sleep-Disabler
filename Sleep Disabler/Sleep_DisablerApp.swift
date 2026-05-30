//
//  Sleep_DisablerApp.swift
//  Sleep Disabler
//

import SwiftUI
import AppKit
import ServiceManagement
import Combine

@main
struct SleepToggleApp: App {

    @NSApplicationDelegateAdaptor(AppDelegate.self)
    var appDelegate

    @StateObject var appState = AppState()

    var body: some Scene {

        WindowGroup {

            MainView()
                .environmentObject(appState)
                .frame(minWidth: 420, minHeight: 320)
        }
    }
}


// MARK: - APP STATE

final class AppState: ObservableObject {

    @Published var isFullySleepDisabled = false
    @Published var launchAtLoginEnabled = false
    @Published var menuBarMode = false

    private var statusItem: NSStatusItem?
    private var refreshTimer: Timer?

    // Prevents refresh overwriting toggle state mid-change
    private var isTogglingSleep = false

    init() {

        // The '?? false' ensures that on a completely fresh install
        // (like on a friend's laptop), it defaults to Window Mode.
        menuBarMode =
            UserDefaults.standard.object(forKey: "menuBarMode") as? Bool ?? false

        updateActivationPolicy()

        refreshAll()

        if menuBarMode {
            createMenuBar()
            
            // Ensures the main window is hidden if the user previously saved Menu Bar mode
            DispatchQueue.main.async {
                self.hideAllWindows()
            }
        }

        startRefreshTimer()
    }

    // MARK: - TIMER

    func startRefreshTimer() {

        refreshTimer =
            Timer.scheduledTimer(
                withTimeInterval: 30,
                repeats: true
            ) { _ in

                self.refreshSleepState()
                self.refreshLaunchState()
            }
    }

    // MARK: - STATE REFRESH

    func refreshAll() {
        refreshSleepState()
        refreshLaunchState()
    }

    // MARK: - MODE

    func toggleMode() {

        menuBarMode.toggle()

        UserDefaults.standard.set(menuBarMode, forKey: "menuBarMode")

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

        NSApp.setActivationPolicy(
            menuBarMode ? .accessory : .regular
        )
    }

    // MARK: - WINDOW

    func showWindow() {

        // 🔥 FIX: Maintain the current mode's policy instead of forcing .regular.
        // This prevents the Dock icon from spawning entirely when pulled from Menu Bar mode.
        updateActivationPolicy()

        NSApplication.shared.activate(ignoringOtherApps: true)

        for window in NSApplication.shared.windows {

            if window.styleMask.contains(.titled) {

                if window.isMiniaturized {
                    window.deminiaturize(nil)
                }

                window.makeKeyAndOrderFront(nil)
                window.orderFrontRegardless()
            }
        }
    }

    func hideAllWindows() {

        for window in NSApplication.shared.windows {
            window.orderOut(nil)
        }

        // 🔥 FIX: Cleanly route through the standard activation manager
        updateActivationPolicy()
    }

    // MARK: - MENU BAR

    func createMenuBar() {

        if statusItem != nil { return }

        statusItem =
            NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)

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

        statusItem?.button?.image =
            NSImage(
                systemSymbolName:
                    isFullySleepDisabled
                    ? "lock.open.fill"
                    : "lock.fill",
                accessibilityDescription: nil
            )
    }

    // MARK: - MENU ACTIONS

    @objc func toggleSleepMenu() {
        toggleSleep()
    }

    @objc func showWindowMenu() {
        showWindow()
    }

    @objc func toggleModeMenu() {
        toggleMode()
    }

    @objc func toggleLoginMenu() {
        toggleLaunchAtLogin()
    }

    @objc func quitMenu() {
        NSApp.terminate(nil)
    }

    // MARK: - SLEEP STATE (FIXED STABLE VERSION)

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

            // Checks for 'sleep 0' (most reliable) OR 'disablesleep 1'.
            let isDisabled =
                output.range(
                    of: #"(?m)^\s*(sleep\s+0|disablesleep\s+1)\b"#,
                    options: .regularExpression
                ) != nil

            DispatchQueue.main.async {

                // Prevent race condition overwrite during toggle
                guard self.isTogglingSleep == false else { return }

                self.isFullySleepDisabled = isDisabled

                self.updateMenuBarIcon()
                self.refreshMenuBar()
            }

        } catch {
            print(error)
        }
    }

    // MARK: - TOGGLE (FIXED RACE CONDITION)

    func toggleSleep() {

        isTogglingSleep = true

        let enabling = !isFullySleepDisabled

        if enabling {

            runCommand(["disablesleep", "1"])
            runCommand(["sleep", "0"])
            runCommand(["displaysleep", "0"])

        } else {

            runCommand(["disablesleep", "0"])
            runCommand(["sleep", "10"])
            runCommand(["displaysleep", "10"])
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

    // MARK: - LOGIN

    func refreshLaunchState() {

        launchAtLoginEnabled =
            SMAppService.mainApp.status == .enabled
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
            print(error)
        }
    }

    // MARK: - RUN COMMAND

    func runCommand(_ args: [String]) {

        let process = Process()

        process.executableURL = URL(
            fileURLWithPath: "/usr/bin/sudo"
        )

        process.arguments = [
            "/usr/bin/pmset",
            "-a"
        ] + args

        do {
            try process.run()
            process.waitUntilExit()

            print("sudo pmset exit:", process.terminationStatus)

        } catch {
            print(error)
        }
    }
}


// MARK: - APP DELEGATE

final class AppDelegate: NSObject, NSApplicationDelegate {

    func applicationDidFinishLaunching(_ notification: Notification) {

        DispatchQueue.main.async {
            self.checkPmsetPermission()
        }
    }

    func checkPmsetPermission() {

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/sudo")
        process.arguments = ["-n", "/usr/bin/pmset", "-g"]

        do {
            try process.run()
            process.waitUntilExit()

            if process.terminationStatus == 0 {
                return
            }

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

        let cmd =
        """
        sudo mkdir -p /etc/sudoers.d && \
        sudo cp \(temp) /etc/sudoers.d/sleep_disabler && \
        sudo chmod 440 /etc/sudoers.d/sleep_disabler
        """

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-c", cmd]

        try? p.run()
        p.waitUntilExit()

        try? FileManager.default.removeItem(atPath: temp)
    }
}
