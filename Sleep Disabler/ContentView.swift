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

                if appState.failsafeEnabled {
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
                Toggle(isOn: $appState.lidDimmerEnabled) {
                    Text("Lid-Closed Dimmer (When Disabled)")
                        .fontWeight(.medium)
                }

                if appState.lidDimmerEnabled {
                    Text("Turns display brightness to 0% when laptop lid is closed to save battery, restoring it when opened.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider()

            Button {
                appState.toggleLaunchAtLogin()
            } label: {
                Text(appState.launchAtLoginEnabled ? "Disable Login Item" : "Enable Login Item")
                    .frame(minWidth: 160)
            }

            Divider()

            Button {
                appState.toggleMode()
            } label: {
                Text(appState.menuBarMode ? "Switch to Window Mode" : "Switch to Menu Bar Mode")
                    .frame(minWidth: 180)
            }

            Spacer()
        }
        .padding(20)
        .frame(minWidth: 420, maxWidth: 460, minHeight: 460)
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
