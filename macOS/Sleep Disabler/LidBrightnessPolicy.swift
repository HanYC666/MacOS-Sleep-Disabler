import Foundation

/// Keeps a pre-close brightness value until the built-in display is online
/// and the restored level has been read back. Writes alone are not confirmation.
struct LidBrightnessPolicy {
    enum RecoveryStep: Equatable {
        case wait
        case restore(Float)
        case complete
        case failed
    }

    private(set) var savedBrightness: Float?
    private(set) var savedDisplay: UInt32?
    private(set) var isDimmed = false
    private(set) var isRecovering = false
    private(set) var restoreFailed = false
    private var recoveryDeadline: Date?

    mutating func rememberOpenBrightness(_ value: Float, display: UInt32) {
        guard !isDimmed, !isRecovering,
              value.isFinite, value > 0, value <= 1 else { return }
        if restoreFailed {
            guard let target = savedBrightness,
                  value >= target - max(0.02, target * 0.15) else { return }
            restoreFailed = false
        }
        savedBrightness = value
        savedDisplay = display
    }

    var canDim: Bool { savedBrightness != nil && savedDisplay != nil && !restoreFailed }

    mutating func didDim() {
        guard canDim else { return }
        isDimmed = true
        isRecovering = false
        recoveryDeadline = nil
    }

    mutating func beginRecovery(at now: Date) {
        guard isDimmed, !isRecovering else { return }
        isRecovering = true
        recoveryDeadline = now.addingTimeInterval(30)
    }

    mutating func lidClosedAgain() {
        isRecovering = false
        recoveryDeadline = nil
    }

    mutating func recoveryStep(readBrightness: Float?, displayOnline: Bool, at now: Date) -> RecoveryStep {
        guard isRecovering, let target = savedBrightness,
              let deadline = recoveryDeadline else { return .wait }
        guard displayOnline else { return now >= deadline ? fail() : .wait }

        let tolerance = max(0.02, target * 0.15)
        if let current = readBrightness, current.isFinite,
           current >= target - tolerance {
            // A confirmed restore (or a brighter user-selected level) ends
            // ownership. Later brightness changes belong to the user/macOS.
            finishRecovery()
            return .complete
        }

        return now >= deadline ? fail() : .restore(target)
    }

    private mutating func finishRecovery() {
        isRecovering = false
        isDimmed = false
        recoveryDeadline = nil
        savedBrightness = nil
        savedDisplay = nil
    }

    private mutating func fail() -> RecoveryStep {
        restoreFailed = true
        isRecovering = false
        isDimmed = false
        recoveryDeadline = nil
        // Keep the known good value; a near-zero read must not replace it.
        return .failed
    }
}
