//
//  ContentView.swift
//  Sleep Disabler
//
//  Created by YC H on 5/27/26.
//

import SwiftUI

struct MainView: View {

    @EnvironmentObject var appState: AppState

    var body: some View {

        VStack(spacing: 18) {

            Text("Sleep Disabler")
                .font(.title)

            Text(
                appState.isFullySleepDisabled
                ? "Sleep Disabled"
                : "Sleep Enabled"
            )

            Button {

                appState.toggleSleep()

            } label: {

                Text(
                    appState.isFullySleepDisabled
                    ? "Enable Sleep"
                    : "Disable Sleep"
                )
            }

            Divider()

            Button {

                appState.toggleLaunchAtLogin()

            } label: {

                Text(
                    appState.launchAtLoginEnabled
                    ? "Disable Login Item"
                    : "Enable Login Item"
                )
            }

            Divider()

            Button {

                appState.toggleMode()

            } label: {

                Text(
                    appState.menuBarMode
                    ? "Switch to Window Mode"
                    : "Switch to Menu Bar Mode"
                )
            }

            Button("Show Window") {
                appState.showWindow()
            }

            Spacer()
        }
        .padding(25)
    }
}
