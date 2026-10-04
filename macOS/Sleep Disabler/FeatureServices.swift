//
//  FeatureServices.swift
//  Sleep Disabler
//

import AppKit
import Darwin
import Foundation
import IOKit

struct HardwareCapabilities: Equatable {
    let hasInternalBattery: Bool
    let hasBuiltInDisplay: Bool

    var supportsLidState: Bool { hasBuiltInDisplay }
    var supportsBrightnessDimming: Bool { hasBuiltInDisplay }

    static func detect(hasInternalBattery: Bool) -> HardwareCapabilities {
        var displays = [CGDirectDisplayID](repeating: 0, count: 16)
        var displayCount: UInt32 = 0
        let result = CGGetOnlineDisplayList(UInt32(displays.count), &displays, &displayCount)
        let hasBuiltInDisplay = result == .success && displays.prefix(Int(displayCount)).contains(where: { CGDisplayIsBuiltin($0) != 0 })
        return HardwareCapabilities(hasInternalBattery: hasInternalBattery, hasBuiltInDisplay: hasBuiltInDisplay)
    }
}

final class BrightnessManager {
    // DisplayServices is private. Dynamic lookup keeps the optional dimmer from
    // becoming a link-time dependency or affecting any non-dimmer feature.
    private typealias GetBrightness = @convention(c) (CGDirectDisplayID, UnsafeMutablePointer<Float>) -> Int32
    private typealias SetBrightness = @convention(c) (CGDirectDisplayID, Float) -> Int32

    private let libraryHandle: UnsafeMutableRawPointer?
    private let getBrightness: GetBrightness?
    private let setBrightness: SetBrightness?
    private var originalBrightness: Float?
    private(set) var isDimmed = false

    var isAvailable: Bool { getBrightness != nil && setBrightness != nil }

    init() {
        let path = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
        guard let libraryHandle = dlopen(path, RTLD_LAZY),
              let getSymbol = dlsym(libraryHandle, "DisplayServicesGetBrightness"),
              let setSymbol = dlsym(libraryHandle, "DisplayServicesSetBrightness") else {
            self.libraryHandle = nil
            self.getBrightness = nil
            self.setBrightness = nil
            return
        }
        self.libraryHandle = libraryHandle
        self.getBrightness = unsafeBitCast(getSymbol, to: GetBrightness.self)
        self.setBrightness = unsafeBitCast(setSymbol, to: SetBrightness.self)
    }

    deinit {
        if let libraryHandle { dlclose(libraryHandle) }
    }

    func dimIfPossible(capabilities: HardwareCapabilities) {
        guard capabilities.supportsBrightnessDimming, let getBrightness, let setBrightness, !isDimmed else { return }
        let display = CGMainDisplayID()
        var brightness: Float = 0
        guard getBrightness(display, &brightness) == 0 else { return }
        guard setBrightness(display, 0) == 0 else { return }
        originalBrightness = brightness
        isDimmed = true
    }

    func restore() {
        defer {
            originalBrightness = nil
            isDimmed = false
        }
        guard let originalBrightness, let setBrightness else { return }
        _ = setBrightness(CGMainDisplayID(), originalBrightness)
    }
}

final class LidMonitor {
    var didChange: ((Bool) -> Void)?

    private var rootDomain: io_service_t = IO_OBJECT_NULL
    private var notificationPort: IONotificationPortRef?
    private var interestNotification: io_object_t = IO_OBJECT_NULL
    private var interestSource: CFRunLoopSource?
    private var workspaceObservers: [NSObjectProtocol] = []
    private var fallbackTimer: Timer?

    init() {
        rootDomain = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        installInterestNotification()
        installWorkspaceNotifications()
        refresh()
    }

    deinit {
        fallbackTimer?.invalidate()
        workspaceObservers.forEach(NSWorkspace.shared.notificationCenter.removeObserver)
        if interestNotification != IO_OBJECT_NULL { IOObjectRelease(interestNotification) }
        if rootDomain != IO_OBJECT_NULL { IOObjectRelease(rootDomain) }
        if let notificationPort {
            if let interestSource { CFRunLoopRemoveSource(CFRunLoopGetMain(), interestSource, .commonModes) }
            IONotificationPortDestroy(notificationPort)
        }
    }

    /// Enables the direct-IOKit fallback only while a lid-dependent feature is active.
    func setMonitoringActive(_ active: Bool) {
        if active {
            refresh()
            guard fallbackTimer == nil else { return }
            fallbackTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
        } else {
            fallbackTimer?.invalidate()
            fallbackTimer = nil
        }
    }

    func currentLidClosed() -> Bool { readLidClosed() }

    func refresh() { didChange?(readLidClosed()) }

    private func installInterestNotification() {
        guard rootDomain != IO_OBJECT_NULL else { return }
        let port = IONotificationPortCreate(kIOMainPortDefault)
        var notification: io_object_t = IO_OBJECT_NULL
        let result = IOServiceAddInterestNotification(
            port,
            rootDomain,
            kIOGeneralInterest,
            { context, _, _, _ in
                guard let context else { return }
                let monitor = Unmanaged<LidMonitor>.fromOpaque(context).takeUnretainedValue()
                DispatchQueue.main.async { monitor.refresh() }
            },
            Unmanaged.passUnretained(self).toOpaque(),
            &notification
        )
        guard result == KERN_SUCCESS else {
            IONotificationPortDestroy(port)
            return
        }
        notificationPort = port
        interestNotification = notification
        interestSource = IONotificationPortGetRunLoopSource(port).takeUnretainedValue()
        if let interestSource { CFRunLoopAddSource(CFRunLoopGetMain(), interestSource, .commonModes) }
    }

    private func installWorkspaceNotifications() {
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didWakeNotification, NSWorkspace.willSleepNotification] {
            workspaceObservers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in self?.refresh() })
        }
    }

    private func readLidClosed() -> Bool {
        guard rootDomain != IO_OBJECT_NULL,
              let property = IORegistryEntryCreateCFProperty(rootDomain, "AppleClamshellState" as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() else {
            return false
        }
        if let bool = property as? Bool { return bool }
        if let number = property as? NSNumber { return number.boolValue }
        return false
    }
}

final class Scheduler {
    var didTick: ((TimeInterval) -> Void)?
    var didFinish: (() -> Void)?

    private var timer: Timer?
    private var endDate: Date?
    private(set) var isRunning = false

    func start(duration: TimeInterval) {
        stop()
        guard duration > 0 else { return }
        isRunning = true
        endDate = Date().addingTimeInterval(duration)
        tick()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        endDate = nil
        isRunning = false
        didTick?(0)
    }

    private func tick() {
        guard let endDate else { return }
        let remaining = endDate.timeIntervalSinceNow
        guard remaining > 0 else {
            stop()
            didFinish?()
            return
        }
        didTick?(remaining)
    }
}
