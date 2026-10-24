// Pure policy: one request per low-battery episode, with fresh dispatch checks.
struct BatteryFailsafePolicy {
    private(set) var triggered = false
    private(set) var generation = 0

    mutating func reset() {
        triggered = false
        generation += 1
    }

    mutating func observe(onBattery: Bool?, percentage: Int?, threshold: Int) {
        // Unknown power data and equality do not rearm a consumed episode.
        if onBattery == false || percentage.map({ $0 > threshold }) == true {
            if triggered { reset() }
        }
    }

    func permitsDispatch(enabled: Bool, onBattery: Bool?, percentage: Int?, threshold: Int) -> Bool {
        enabled && onBattery == true && percentage.map({ $0 < threshold }) == true
    }

    mutating func consume(enabled: Bool, preventionActive: Bool,
                          onBattery: Bool?, percentage: Int?, threshold: Int) -> Int? {
        observe(onBattery: onBattery, percentage: percentage, threshold: threshold)
        guard !triggered, preventionActive,
              permitsDispatch(enabled: enabled, onBattery: onBattery,
                              percentage: percentage, threshold: threshold) else { return nil }
        triggered = true
        generation += 1
        return generation
    }
}
