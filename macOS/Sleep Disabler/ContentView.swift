//
//  ContentView.swift
//  Sleep Disabler
//

import SwiftUI
import AppKit

struct CustomStepper: NSViewRepresentable {
    @Binding var value: Int
    var range: ClosedRange<Int>

    func makeNSView(context: Context) -> NSStepper {
        let stepper = NSStepper()
        stepper.minValue = Double(range.lowerBound)
        stepper.maxValue = Double(range.upperBound)
        stepper.increment = 1
        stepper.autorepeat = true
        stepper.isContinuous = true
        stepper.valueWraps = false
        stepper.target = context.coordinator
        stepper.action = #selector(Coordinator.valueChanged(_:))
        return stepper
    }

    func updateNSView(_ nsView: NSStepper, context: Context) {
        nsView.integerValue = value
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    class Coordinator: NSObject {
        var parent: CustomStepper
        init(_ parent: CustomStepper) {
            self.parent = parent
        }
        @objc func valueChanged(_ sender: NSStepper) {
            parent.value = sender.integerValue
        }
    }
}

struct MainView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        ScrollView {
            settingsContent.padding(20)
        }
        .frame(minWidth: 420, maxWidth: 460, minHeight: 600)
    }

    private var settingsContent: some View {
        VStack(spacing: 16) {
            Text("Sleep Disabler")
                .font(.title)
                .bold()

            Text(appState.isFullySleepDisabled ? "Sleep Disabled" : "Sleep Enabled")
                .font(.subheadline)
                .foregroundColor(appState.isFullySleepDisabled ? .orange : .secondary)

            Button {
                appState.toggleSleep()
            } label: {
                Text(appState.isFullySleepDisabled ? "Enable Sleep" : "Disable Sleep")
                    .frame(minWidth: 140)
            }
            .buttonStyle(.borderedProminent)
            .tint(appState.isFullySleepDisabled ? .green : .blue)

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                Toggle(isOn: $appState.failsafeEnabled) {
                    Text("Battery Power Failsafe")
                        .fontWeight(.medium)
                }
                .disabled(!appState.supportsBatteryFailsafe)

                if !appState.supportsBatteryFailsafe {
                    Text("Available when this Mac has an internal battery.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                } else if appState.failsafeEnabled {
                    Text("Restores sleep settings and puts the Mac to sleep if battery percentage falls below this value.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack {
                        Text("Critical battery level: \(appState.failsafeThreshold)%")
                            .font(.body)
                        Spacer()
                        CustomStepper(value: $appState.failsafeThreshold, range: 1...99)
                    }
                    .padding(.top, 2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                Toggle(isOn: $appState.sleepTimerRunning) {
                    Text(appState.sleepTimerRunning ? "Sleep Scheduler Running" : "Sleep Scheduler")
                        .fontWeight(.medium)
                }
                .toggleStyle(.switch)
                .disabled(!appState.sleepTimerRunning && !appState.hasSleepTimerDuration)

                Text(appState.sleepTimerRunning
                     ? "Your Mac will sleep in \(appState.sleepTimerRemainingText)."
                     : "Puts your Mac to sleep after the selected duration.")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 18) {
                    timerValue("Days", value: $appState.sleepTimerDays, range: 0...365)
                    timerValue("Hours", value: $appState.sleepTimerHours, range: 0...23)
                    timerValue("Minutes", value: $appState.sleepTimerMinutes, range: 0...59)
                }

                Toggle("Only enable when lid is closed", isOn: $appState.sleepTimerLidClosedOnly)
                    .font(.caption)
                    .toggleStyle(.checkbox)

                Toggle("Only enable when sleep is disabled", isOn: $appState.sleepTimerSleepDisabledOnly)
                    .font(.caption)
                    .toggleStyle(.checkbox)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            GroupBox {
                VStack(alignment: .leading, spacing: 12) {
                    lidBehaviorRow(
                        "Lock Screen When Lid Closes",
                        symbol: "lock",
                        description: appState.supportsLidLock
                            ? "Locks your session when you close the lid."
                            : "Unavailable on this Mac or macOS version.",
                        isOn: $appState.lockOnLidClose,
                        available: appState.supportsLidLock
                    )

                    Divider()

                    lidBehaviorRow(
                        "Dim Display When Lid Closes",
                        symbol: "sun.min",
                        description: appState.supportsLidDimmer
                            ? "Sets brightness to zero and restores it when opened."
                            : "Requires a MacBook with brightness control.",
                        isOn: $appState.lidDimmerEnabled,
                        available: appState.supportsLidDimmer
                    )

                    Text("Applies only while sleep prevention is active.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(6)
            } label: {
                Text("Lid Behavior")
                    .fontWeight(.medium)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Launch at Login")
                        .fontWeight(.medium)
                    Text("Starts Sleep Disabler when you sign in.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Toggle("Launch at Login", isOn: Binding(
                    get: { appState.launchAtLoginEnabled },
                    set: { enabled in
                        if enabled != appState.launchAtLoginEnabled {
                            appState.toggleLaunchAtLogin()
                        }
                    }
                ))
                .labelsHidden()
            }

            Divider()

            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("App Mode")
                        .fontWeight(.medium)
                    Text(appState.menuBarMode ? "Runs from the menu bar." : "Shows the main window.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Picker("App Mode", selection: Binding(
                    get: { appState.menuBarMode },
                    set: { enabled in
                        if enabled != appState.menuBarMode {
                            appState.toggleMode()
                        }
                    }
                )) {
                    Text("Window").tag(false)
                    Text("Menu Bar").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 156)
            }
        }
    }

    private func lidBehaviorRow(
        _ title: String, symbol: String, description: String,
        isOn: Binding<Bool>, available: Bool
    ) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .foregroundColor(.secondary)
                .frame(width: 20, height: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .fontWeight(.medium)
                Text(description)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Toggle(title, isOn: isOn)
                .toggleStyle(.switch)
                .labelsHidden()
                .disabled(!available)
                .help("Applies only while sleep prevention is active.")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func timerValue(_ title: String, value: Binding<Int>, range: ClosedRange<Int>) -> some View {
        HStack(spacing: 4) {
            Text("\(title):")
                .frame(width: 54, alignment: .leading)
            Text("\(value.wrappedValue)")
                .monospacedDigit()
                .frame(width: 28, alignment: .trailing)
            CustomStepper(value: value, range: range)
                .frame(width: 19, height: 24)
        }
        .frame(width: 109, alignment: .leading)
    }
}

struct NotificationView: View {
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "exclamationmark.triangle.fill")
                .resizable()
                .frame(width: 44, height: 44)
                .foregroundColor(.yellow)
            
            Text("Critical Battery Level")
                .font(.headline)
            
            Text("Mac was put to sleep because battery fell below the critical threshold.")
                .multilineTextAlignment(.center)
                .font(.subheadline)
                .foregroundColor(.secondary)
        }
        .padding(24)
        .frame(width: 320, height: 180)
    }
}
