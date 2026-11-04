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

        // A successful write can be followed by a delayed macOS reset. Keep
        // the old baseline throughout the settling period and retry the reset.
        expect(policy.recoveryStep(readBrightness: 0.55, displayOnline: true, at: start) == .wait,
               "An immediate matching read does not finish recovery")
        expect(policy.recoveryStep(readBrightness: 0.7, displayOnline: true,
                                   at: start.addingTimeInterval(1)) == .wait,
               "A brighter transient cannot finish recovery early")
        policy.rememberOpenBrightness(0.02, display: 42)
        expect(policy.savedBrightness == 0.55, "A transient near-zero reading cannot poison the baseline")
        expect(policy.recoveryStep(readBrightness: 0.02, displayOnline: true,
                                   at: start.addingTimeInterval(5)) == .restore(0.55),
               "A late brightness reset retries the original level")
        expect(policy.recoveryStep(readBrightness: nil, displayOnline: false,
                                   at: start.addingTimeInterval(10)) == .wait,
               "An offline built-in display is not treated as restored")
        expect(policy.recoveryStep(readBrightness: 0.55, displayOnline: true,
                                   at: start.addingTimeInterval(30)) == .complete,
               "Recovery finishes after the settling interval")
        expect(!policy.isDimmed && policy.savedBrightness == nil, "Completion permits a fresh baseline")

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
                                         at: opened.addingTimeInterval(30)) == .complete,
                   "Each repeated cycle recovers")
        }

        print("Passed \(checks) lid brightness policy checks; no display brightness was changed.")
    }
}
