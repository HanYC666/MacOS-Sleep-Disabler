import Foundation

@main
enum LidBrightnessPolicyTests {
    static func main() {
        var checks = 0
        func expect(_ condition: Bool, _ message: String) {
            precondition(condition, message)
            checks += 1
        }

        let start = Date(timeIntervalSince1970: 1_000)
        var policy = LidBrightnessPolicy()
        policy.rememberOpenBrightness(0.55, display: 42)
        expect(policy.canDim, "A valid open-lid reading permits dimming")
        policy.didDim()
        policy.beginRecovery(at: start)

        // A zero read while the display comes online needs a restore. Once
        // the target is read back, further user brightness changes are free.
        expect(policy.recoveryStep(readBrightness: nil, displayOnline: false, at: start) == .wait,
               "An offline built-in display is not treated as restored")
        policy.rememberOpenBrightness(0.02, display: 42)
        expect(policy.savedBrightness == 0.55, "A transient near-zero reading cannot poison the baseline")
        expect(policy.recoveryStep(readBrightness: 0.02, displayOnline: true,
                                   at: start.addingTimeInterval(5)) == .restore(0.55),
               "An unverified low brightness retries the original level")
        expect(policy.recoveryStep(readBrightness: 0.55, displayOnline: true,
                                   at: start.addingTimeInterval(5.5)) == .complete,
               "A readback of the restored level finishes recovery immediately")
        expect(!policy.isDimmed && policy.savedBrightness == nil, "Completion permits a fresh baseline")
        expect(policy.recoveryStep(readBrightness: 0.25, displayOnline: true,
                                   at: start.addingTimeInterval(6)) == .wait,
               "Lowering brightness after verification never restores the old value")
        policy.rememberOpenBrightness(0.25, display: 42)
        expect(policy.savedBrightness == 0.25, "A manual reduction becomes the next baseline")

        var brighter = LidBrightnessPolicy()
        brighter.rememberOpenBrightness(0.4, display: 42)
        brighter.didDim()
        brighter.beginRecovery(at: start)
        expect(brighter.recoveryStep(readBrightness: 0.7, displayOnline: true,
                                    at: start) == .complete,
               "A brighter level ends recovery without a write")

        policy.rememberOpenBrightness(0.4, display: 42)
        policy.didDim()
        policy.beginRecovery(at: start)
        expect(policy.recoveryStep(readBrightness: 0.01, displayOnline: true,
                                   at: start.addingTimeInterval(30)) == .failed,
               "Persistent low brightness disables further dimming")
        policy.rememberOpenBrightness(0.01, display: 42)
        expect(policy.savedBrightness == 0.4 && !policy.canDim,
               "Failure preserves the known good level and blocks another dim")
        policy.rememberOpenBrightness(0.4, display: 42)
        expect(policy.canDim, "Dimming resumes after the user restores a safe brightness")

        var repeated = LidBrightnessPolicy()
        for cycle in 0..<5 {
            repeated.rememberOpenBrightness(0.6, display: 42)
            repeated.didDim()
            let opened = start.addingTimeInterval(Double(cycle) * 40)
            repeated.beginRecovery(at: opened)
            repeated.lidClosedAgain()
            repeated.didDim()
            repeated.beginRecovery(at: opened)
            expect(repeated.recoveryStep(readBrightness: 0.04, displayOnline: true,
                                         at: opened.addingTimeInterval(8)) == .restore(0.6),
                   "Rapid close/open keeps the original level")
            expect(repeated.recoveryStep(readBrightness: 0.6, displayOnline: true,
                                         at: opened.addingTimeInterval(8.5)) == .complete,
                   "Each repeated cycle completes after a verified restore")
        }

        print("Passed \(checks) lid brightness policy checks; no display brightness was changed.")
    }
}
