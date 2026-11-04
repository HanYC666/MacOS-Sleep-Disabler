# Sleep Disabler

The current archives prevent repeated battery-failsafe sleep within one low-battery episode, including after resume or re-enabling prevention. Confirmed AC power, battery recovery above the threshold, or changing failsafe settings rearms it. The app checks power again immediately before sending the sleep command. This protection lasts for the app process; restarting the app starts a new episode.

Scheduled sleep sends a **Sleep Disabler — 1 minute until sleep** system notification once in the final minute. Allow notifications when first starting a timer and select Banners in macOS notification settings; Focus and system settings can suppress presentation. Cancel the timer in the app to keep working. Critical-battery failsafe sleep remains immediate, even during activity. Both native ZIPs include this warning.

The most lightweight macOS app that lets you quickly stop your Mac from sleeping or turning off its display.

It is written in SwiftUI and can run either as a normal window or as a menu bar app. It is useful for long-running downloads, scripts, builds, or agentic tasks where the Mac needs to stay awake.

Ram usage: 27.6 MB with all functions turned on.

## What it does

- Turn sleep and display sleep on or off.
- Run from a regular window or from the menu bar.
- Remember your selected mode after restarting the app.
- Optionally launch when you log in.
- Show the current state with a menu bar icon.
- Check the power settings regularly so the displayed state stays up to date.
- Read battery percentage and the active power source through IOKit instead of parsing `pmset` text.
- **Dynamic State Saving**: Remembers exactly what your Mac's sleep settings were before disabling sleep, and perfectly restores them when re-enabled.
- **Battery Failsafe**: Let's you set a custom battery percentage (like 5%). If the battery drops below this while sleep is disabled, the app saves your Mac by forcing it to sleep and dropping a notification window.
- **Lid-Closed Dimmer**: When sleep is disabled, closing a MacBook lid sets its built-in display brightness to 0. The app saves the brightness while the lid is open and restores that value when it opens again. It checks the restored level for 30 seconds because macOS can reset the display after reopening; repeated failures pause dimming until brightness recovers.
- **Lock Screen When Lid Closes**: Optionally lock your MacBook when you close its lid while sleep prevention is active. Find the switch under **Lid Behavior** or in the menu-bar menu. The preference is saved and starts off for existing and new installations.
- **Sleep Scheduler**: Set a days, hours, and minutes countdown to put your Mac to sleep. The last duration and its safety options are saved, and menu-bar mode provides a quick start action plus a live remaining-time display.

## Requirements

- macOS 13.5 or newer
- Apple Silicon or Intel Mac
- Xcode Command Line Tools (for building from source)

## Install

Download `Sleep-Disabler-arm64.zip` for Apple Silicon or `Sleep-Disabler-x86_64.zip` for Intel. Each archive contains a separate native `.app`; neither is a universal binary.

Extract the matching archive and copy `Sleep Disabler.app` into Applications. For an upgrade, quit the existing app before replacing it, and keep its preferences so saved settings remain available. Copying an app does not launch it; open it when ready to use it. Both ZIPs include the scheduled-sleep warning, battery-failsafe repeat protection, optional lid locking, and the window and brightness fixes. Follow [compile.md](compile.md) to build from source, then copy the matching app from `build/native-archives` into Applications.

On the first launch, the app opens in Window Mode. Later launches use the saved mode. Menu Bar mode creates the main window only when you choose **Show Window** or switch to Window Mode.

In **Lid Behavior**, enable **Lock Screen When Lid Closes** if desired. It starts off and remembers subsequent changes. **Disable Sleep** activates prevention; the lid settings apply only while prevention is active. Dimming and locking are independent. Menu Bar mode offers the same lock preference as a checked menu item. A disabled switch means this Mac or macOS version lacks the required lid or locking capability. Locking needs neither an extra `sudoers` rule nor simulated keyboard shortcuts.

## Permissions

Sleep Disabler uses macOS's `pmset` command to change power settings. Its state-changing commands require administrator permission.

At startup, the app verifies its restricted password-free permission with a read-only `sudo -ll` policy listing. If the rule is missing or the app's older unrestricted rule is present, it explains why administrator authorization is needed before macOS requests the password. It validates the replacement with `visudo`, never reads or stores the password, and refuses to overwrite an unrecognized file.

```text
%admin ALL=(root) NOPASSWD: /usr/bin/pmset ^-a disablesleep [01]$, /usr/bin/pmset ^-a sleep 0$, /usr/bin/pmset ^-a displaysleep 0$, /usr/bin/pmset ^-[bc] (sleep|displaysleep) (0|[1-9][0-9]?|1[0-7][0-9]|180)$, /usr/bin/pmset ^sleepnow$
```

This permits only the exact disable, restore, and immediate-sleep operations used by the app. macOS may request administrator authorization when installing or replacing the protected file; the old broad `pmset` rule cannot itself authorize that file change.

### Manual setup

Open Terminal and run:

```bash
sudo EDITOR=nano visudo -f /etc/sudoers.d/sleep_disabler
```

Add this exact line:

```text
%admin ALL=(root) NOPASSWD: /usr/bin/pmset ^-a disablesleep [01]$, /usr/bin/pmset ^-a sleep 0$, /usr/bin/pmset ^-a displaysleep 0$, /usr/bin/pmset ^-[bc] (sleep|displaysleep) (0|[1-9][0-9]?|1[0-7][0-9]|180)$, /usr/bin/pmset ^sleepnow$
```

Save the file in Nano with `Control + O`, press `Enter`, then exit with `Control + X`.

Finally, set the file permissions:

```bash
sudo chmod 440 /etc/sudoers.d/sleep_disabler
```

Only add a `sudoers` rule if you understand what it does. If you want to remove it, use an administrator-authorized file-management method you are comfortable with.

## Building from source

From the repository root, run `cd macOS`, then `zsh Scripts/archive-native.sh` without `sudo`. The script uses only Xcode Command Line Tools and creates separate Apple Silicon and Intel apps. See [RELEASE.md](RELEASE.md) and [compile.md](compile.md).

The deployment target is macOS 13.5+ by project default.

## How it works

When sleep is disabled, the app first runs `pmset -g custom` behind the scenes. It grabs your exact settings for both Battery and AC power and saves them to `UserDefaults`. Then it runs these commands through `pmset` to keep the Mac awake:

```bash
sudo pmset -a disablesleep 1
sudo pmset -a sleep 0
sudo pmset -a displaysleep 0
```

When sleep is enabled again, instead of resetting to a hardcoded default, it restores your exact original values perfectly using your saved states:

```bash
sudo pmset -a disablesleep 0
sudo pmset -b sleep <saved_battery_sleep>
sudo pmset -b displaysleep <saved_battery_displaysleep>
sudo pmset -c sleep <saved_ac_sleep>
sudo pmset -c displaysleep <saved_ac_displaysleep>
```

For the **Battery Failsafe**, the app uses IOKit Power Sources values and change notifications. It checks the system's current power provider separately, so charging or AC operation cannot be mistaken for battery discharge.

For the **Lid-Closed Dimmer**, the app reads `AppleClamshellState` directly through IOKit and listens for general IOKit and workspace power events. Apple does not expose a supported lid-specific event API, so a direct IOKit fallback check runs only while a lid-dependent feature is active. The control is greyed out unless the Apple-reported model name contains `MacBook` and brightness control is available.

**Lock Screen When Lid Closes** shares that lid monitor but works independently of brightness control. It requests a lock only on an observed open-to-closed transition while prevention and the option are both on. Startup, enabling the option with an already-closed lid, unreadable sensor data, and repeated closed readings do not trigger locks. Locking uses the dynamically resolved private `SACLockScreenImmediate` function in `login.framework`, also used by [Hammerspoon](https://github.com/Hammerspoon/hammerspoon/blob/master/extensions/caffeinate/libcaffeinate.m). The switch is unavailable if lid monitoring or the symbol is unavailable; private APIs may change between macOS releases. This setting does not intercept manual Sleep actions on macOS.

Run `zsh Scripts/test-lid-lock.sh` and `zsh Scripts/test-lid-brightness.sh` from the macOS directory with Xcode Command Line Tools installed to exercise the standalone lid policies. These tests do not change brightness, lock, or suspend the host. Both native architectures compiled successfully; the lid-lock and repeated-cycle brightness policy checks passed. The earlier release was manually tested by the maintainer; the newer window, dimmer, warning, and failsafe changes still need attended hardware checks.

The **Sleep Scheduler** uses the selected days, hours, and minutes as a countdown and then runs `pmset sleepnow`. Its saved values are restored at launch, so the menu-bar quick start always uses the last configured schedule. While a schedule is active, the menu displays the live remaining time. The optional closed-lid condition prevents starting while the lid is open and cancels the schedule if the lid opens. The optional sleep-disabled condition only permits a schedule while Sleep Disabler is active, and cancels it when normal sleep is restored.

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

You may use, modify, redistribute, repackage, fork, or build upon this project in accordance with the AGPL-3.0.

Any modified, redistributed, repackaged, hosted, or derivative version must:

- remain fully open source under the AGPL-3.0;
- make the corresponding source code available as required by the license;
- preserve all copyright, license, and attribution notices;
- must clearly credit the original author, **HanYC666**;
- not misrepresent the original authorship of the project.

Original author: **HanYC666**

All contents of this project come with **absolutely no warranty**.
