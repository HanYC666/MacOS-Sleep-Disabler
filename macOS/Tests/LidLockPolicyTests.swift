// Standalone tests: compile with LidLockPolicy.swift. No AppKit, IOKit,
// screen-lock calls, or sleep operations are linked into this executable.
@main
enum LidLockPolicyTests {
    static func main() {
        var checks = 0
        func expect(_ value: Bool, _ message: String) {
            precondition(value, message)
            checks += 1
        }

        for enabled in [false, true] {
            for active in [false, true] {
                var policy = LidLockPolicy()
                expect(!policy.shouldLock(lidClosed: false, preventionActive: active, enabled: enabled), "Initial open baseline")
                expect(policy.shouldLock(lidClosed: true, preventionActive: active, enabled: enabled) == (enabled && active), "Both settings must permit locking")
                expect(!policy.shouldLock(lidClosed: true, preventionActive: active, enabled: enabled), "No repeated lock while closed")
                expect(!policy.shouldLock(lidClosed: false, preventionActive: active, enabled: enabled), "Opening never locks")
                expect(policy.shouldLock(lidClosed: true, preventionActive: active, enabled: enabled) == (enabled && active), "Next close can lock again")
            }
        }

        let initialStates: [Bool?] = [nil, false, true]
        for firstState in initialStates {
            var policy = LidLockPolicy()
            expect(!policy.shouldLock(lidClosed: firstState, preventionActive: true, enabled: true), "Startup never locks")
            policy.reset()
            expect(!policy.shouldLock(lidClosed: true, preventionActive: true, enabled: true), "Enabling with lid closed never locks")
        }

        var policy = LidLockPolicy()
        _ = policy.shouldLock(lidClosed: false, preventionActive: true, enabled: true)
        expect(!policy.shouldLock(lidClosed: nil, preventionActive: true, enabled: true), "Unreadable lid cannot lock")
        expect(!policy.shouldLock(lidClosed: true, preventionActive: true, enabled: true), "Unreadable data cannot fabricate a transition")

        // AppState resets the policy on option/prevention changes, including
        // changes that happen between two lid samples.
        for _ in 0..<2 {
            _ = policy.shouldLock(lidClosed: false, preventionActive: true, enabled: true)
            policy.reset()
            expect(!policy.shouldLock(lidClosed: true, preventionActive: true, enabled: true), "Reset discards stale open state")
        }
        print("Passed \(checks) lid-lock policy checks; no system APIs called.")
    }
}
