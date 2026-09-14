//
//  SystemServices.swift
//  Sleep Disabler
//

import AppKit
import Foundation
import IOKit
import IOKit.ps

struct CommandResult {
    let status: Int32
    let standardOutput: String
    let standardError: String

    var succeeded: Bool { status == 0 }
}

protocol CommandRunning {
    func run(executable: String, arguments: [String]) -> CommandResult?
}

final class ProcessRunner: CommandRunning {
    func run(executable: String, arguments: [String]) -> CommandResult? {
        let process = Process()
        let output = Pipe()
        let error = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = error

        do {
            try process.run()
            process.waitUntilExit()
            return CommandResult(
                status: process.terminationStatus,
                standardOutput: String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self),
                standardError: String(decoding: error.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            )
        } catch {
            return nil
        }
    }
}

struct PowerSettings: Equatable {
    let batterySleep: Int
    let batteryDisplaySleep: Int
    let acSleep: Int
    let acDisplaySleep: Int
}

protocol PowerManaging: AnyObject {
    func currentSettings() -> PowerSettings?
    func isSleepDisabled() -> Bool?
    func disableSleep() -> Bool
    func restoreSleep(using settings: PowerSettings) -> Bool
    func sleepNow() -> Bool
}

final class PowerManager: PowerManaging {
    private let runner: CommandRunning
    private let defaults: UserDefaults

    init(runner: CommandRunning = ProcessRunner(), defaults: UserDefaults = .standard) {
        self.runner = runner
        self.defaults = defaults
    }

    func currentSettings() -> PowerSettings? {
        if let output = runner.run(executable: "/usr/bin/pmset", arguments: ["-g", "custom"])?.standardOutput,
           let settings = parseSettings(output) {
            return settings
        }
        // macOS 15 can omit the inactive source from `pmset -g custom` despite
        // its documented all-sources behavior. Its structured preferences file
        // retains both sources, so use it only to complete that read failure.
        return readStoredPowerSettings()
    }

    func isSleepDisabled() -> Bool? {
        guard let output = runner.run(executable: "/usr/bin/pmset", arguments: ["-g", "custom"])?.standardOutput else {
            return nil
        }
        return output.range(of: #"(?m)^\s*(sleep\s+0|disablesleep\s+1)\b"#, options: .regularExpression) != nil
    }

    func disableSleep() -> Bool {
        runPrivileged(["-a", "disablesleep", "1"])
            && runPrivileged(["-a", "sleep", "0"])
            && runPrivileged(["-a", "displaysleep", "0"])
    }

    func restoreSleep(using settings: PowerSettings) -> Bool {
        runPrivileged(["-a", "disablesleep", "0"])
            && runPrivileged(["-b", "sleep", String(settings.batterySleep)])
            && runPrivileged(["-b", "displaysleep", String(settings.batteryDisplaySleep)])
            && runPrivileged(["-c", "sleep", String(settings.acSleep)])
            && runPrivileged(["-c", "displaysleep", String(settings.acDisplaySleep)])
    }

    func sleepNow() -> Bool {
        runPrivileged(["sleepnow"])
    }

    func saveCurrentSettingsIfNeeded(isSleepDisabled: Bool) -> PowerSettings? {
        guard let settings = currentSettings() else { return nil }
        guard !isSleepDisabled else { return settings }
        defaults.set(settings.batterySleep, forKey: "origBattSleep")
        defaults.set(settings.batteryDisplaySleep, forKey: "origBattDisplay")
        defaults.set(settings.acSleep, forKey: "origAcSleep")
        defaults.set(settings.acDisplaySleep, forKey: "origAcDisplay")
        return settings
    }

    func savedSettings() -> PowerSettings? {
        guard let batterySleep = defaults.object(forKey: "origBattSleep") as? Int,
              let batteryDisplaySleep = defaults.object(forKey: "origBattDisplay") as? Int,
              let acSleep = defaults.object(forKey: "origAcSleep") as? Int,
              let acDisplaySleep = defaults.object(forKey: "origAcDisplay") as? Int else { return nil }
        let values = [batterySleep, batteryDisplaySleep, acSleep, acDisplaySleep]
        guard values.allSatisfy({ (0...180).contains($0) }) else { return nil }
        return PowerSettings(
            batterySleep: batterySleep,
            batteryDisplaySleep: batteryDisplaySleep,
            acSleep: acSleep,
            acDisplaySleep: acDisplaySleep
        )
    }

    private func runPrivileged(_ arguments: [String]) -> Bool {
        guard isAllowed(arguments) else { return false }
        return runner.run(executable: "/usr/bin/sudo", arguments: ["-n", "/usr/bin/pmset"] + arguments)?.succeeded == true
    }

    private func isAllowed(_ arguments: [String]) -> Bool {
        let fixedCommands = [
            ["sleepnow"], ["-a", "disablesleep", "0"], ["-a", "disablesleep", "1"],
            ["-a", "sleep", "0"], ["-a", "displaysleep", "0"]
        ]
        if fixedCommands.contains(arguments) { return true }
        guard arguments.count == 3,
              ["-b", "-c"].contains(arguments[0]),
              ["sleep", "displaysleep"].contains(arguments[1]),
              let value = Int(arguments[2]) else { return false }
        return (0...180).contains(value)
    }

    private func parseSettings(_ output: String) -> PowerSettings? {
        var section = ""
        var batterySleep: Int?
        var batteryDisplaySleep: Int?
        var acSleep: Int?
        var acDisplaySleep: Int?

        for line in output.split(whereSeparator: \.isNewline) {
            let lower = line.lowercased()
            if lower.contains("battery power:") { section = "battery" }
            if lower.contains("ac power:") { section = "ac" }
            let words = line.split(whereSeparator: \.isWhitespace)
            guard words.count >= 2, let value = Int(words[1]), (0...180).contains(value) else { continue }
            switch (section, words[0].lowercased()) {
            case ("battery", "sleep"): batterySleep = value
            case ("battery", "displaysleep"): batteryDisplaySleep = value
            case ("ac", "sleep"): acSleep = value
            case ("ac", "displaysleep"): acDisplaySleep = value
            default: break
            }
        }

        guard let batterySleep, let batteryDisplaySleep, let acSleep, let acDisplaySleep else { return nil }
        return PowerSettings(
            batterySleep: batterySleep,
            batteryDisplaySleep: batteryDisplaySleep,
            acSleep: acSleep,
            acDisplaySleep: acDisplaySleep
        )
    }

    private func readStoredPowerSettings() -> PowerSettings? {
        let url = URL(fileURLWithPath: "/Library/Preferences/com.apple.PowerManagement.plist")
        guard let data = try? Data(contentsOf: url),
              let preferences = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any],
              let battery = preferences["Battery Power"] as? [String: Any],
              let ac = preferences["AC Power"] as? [String: Any],
              let batterySleep = battery["System Sleep Timer"] as? Int,
              let batteryDisplaySleep = battery["Display Sleep Timer"] as? Int,
              let acSleep = ac["System Sleep Timer"] as? Int,
              let acDisplaySleep = ac["Display Sleep Timer"] as? Int else { return nil }
        let values = [batterySleep, batteryDisplaySleep, acSleep, acDisplaySleep]
        guard values.allSatisfy({ (0...180).contains($0) }) else { return nil }
        return PowerSettings(
            batterySleep: batterySleep,
            batteryDisplaySleep: batteryDisplaySleep,
            acSleep: acSleep,
            acDisplaySleep: acDisplaySleep
        )
    }
}

struct BatteryStatus: Equatable {
    let hasInternalBattery: Bool
    let percentage: Int?
    let isDrawingFromBattery: Bool
}

final class BatteryMonitor {
    var didChange: ((BatteryStatus) -> Void)?
    private var notificationSource: CFRunLoopSource?

    init() {
        refresh()
        notificationSource = IOPSNotificationCreateRunLoopSource({ context in
            guard let context else { return }
            let monitor = Unmanaged<BatteryMonitor>.fromOpaque(context).takeUnretainedValue()
            DispatchQueue.main.async { monitor.refresh() }
        }, Unmanaged.passUnretained(self).toOpaque())?.takeRetainedValue()
        if let notificationSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), notificationSource, .commonModes)
        }
    }

    deinit {
        if let notificationSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), notificationSource, .commonModes)
        }
    }

    func refresh() {
        guard let snapshot = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let sources = IOPSCopyPowerSourcesList(snapshot)?.takeRetainedValue() as? [CFTypeRef] else {
            didChange?(BatteryStatus(hasInternalBattery: false, percentage: nil, isDrawingFromBattery: false))
            return
        }

        var capacity: Int?
        let providingPower = IOPSGetProvidingPowerSourceType(snapshot).takeRetainedValue() as String
        var hasBattery = false
        for source in sources {
            guard let description = IOPSGetPowerSourceDescription(snapshot, source)?.takeUnretainedValue() as? [String: Any] else { continue }
            let type = description[kIOPSTypeKey as String] as? String
            guard type == (kIOPSInternalBatteryType as String) else { continue }
            hasBattery = true
            capacity = description[kIOPSCurrentCapacityKey as String] as? Int
        }
        didChange?(BatteryStatus(
            hasInternalBattery: hasBattery,
            percentage: capacity,
            isDrawingFromBattery: providingPower == (kIOPSBatteryPowerValue as String)
        ))
    }
}
