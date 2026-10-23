// Pure lid-transition policy; deliberately has no power or screen-lock APIs.
struct LidLockPolicy {
    private var previousLidClosed: Bool?

    mutating func reset() {
        previousLidClosed = nil
    }

    mutating func shouldLock(lidClosed: Bool?, preventionActive: Bool, enabled: Bool) -> Bool {
        defer { previousLidClosed = lidClosed }
        // Startup, unreadable state, and an already-closed lid establish a
        // baseline. Only an observed open-to-closed transition requests a lock.
        return enabled && preventionActive && previousLidClosed == false && lidClosed == true
    }
}
