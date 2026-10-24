@main
struct BatteryFailsafePolicyTests {
    static func main() {
        var policy = BatteryFailsafePolicy()
        func consume(_ battery: Bool? = true, _ percentage: Int? = 19) -> Int? {
            policy.consume(enabled: true, preventionActive: true,
                           onBattery: battery, percentage: percentage, threshold: 20)
        }
        let first = consume()!
        // Waking and re-enabling prevention cannot repeat the same episode.
        assert(consume() == nil)
        assert(consume(true, 20) == nil)
        assert(consume(nil, nil) == nil)
        assert(policy.generation == first)
        // A charger attached during restoration blocks the queued dispatch.
        assert(!policy.permitsDispatch(enabled: true, onBattery: false, percentage: 19, threshold: 20))
        assert(!policy.permitsDispatch(enabled: true, onBattery: nil, percentage: 19, threshold: 20))
        assert(!policy.permitsDispatch(enabled: true, onBattery: true, percentage: nil, threshold: 20))
        assert(!policy.permitsDispatch(enabled: true, onBattery: true, percentage: 20, threshold: 20))
        assert(!policy.permitsDispatch(enabled: false, onBattery: true, percentage: 19, threshold: 20))
        assert(policy.permitsDispatch(enabled: true, onBattery: true, percentage: 19, threshold: 20))
        policy.observe(onBattery: false, percentage: 19, threshold: 20)
        assert(policy.generation != first)
        assert(consume(false) == nil)
        assert(consume() != nil) // A new battery episode after AC recovery.
        policy.observe(onBattery: true, percentage: 21, threshold: 20)
        assert(consume() != nil)
        policy.reset() // Explicit settings changes invalidate queued requests.
        assert(!policy.triggered)
        assert(policy.consume(enabled: true, preventionActive: false,
                              onBattery: true, percentage: 19, threshold: 20) == nil)
        print("Passed battery failsafe episode and dispatch policy checks; no power APIs called.")
    }
}
