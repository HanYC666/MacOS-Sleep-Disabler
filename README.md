# Sleep Disabler

The most lightweight macOS app that lets you quickly stop your Mac from sleeping or turning off its display.

It is written in SwiftUI and can run either as a normal window or as a menu bar app. It is useful for long-running downloads, scripts, builds, or agentic tasks where the Mac needs to stay awake.

Ram usage: 27.6 MB with all functions turned on.

## What it does

- Turn sleep and display sleep on or off.
- Run from a regular window or from the menu bar.
- Remember your selected mode after restarting the app.
- Optionally launch when you log in.
- Show the current state with a menu bar icon.
- Read structured power-source changes through IOKit for the battery failsafe.
- **Dynamic State Saving**: Captures the AC and battery timer baseline before disabling sleep, restores only settings this app successfully changed, and refuses to mutate settings when it cannot capture a complete baseline.
- **Battery Failsafe**: Let's you set a custom battery percentage (like 5%). If the battery drops below this while sleep is disabled, the app saves your Mac by forcing it to sleep and dropping a notification window.
- **Lid-Closed Dimmer**: When sleep is disabled, you can close your MacBook lid to drop the screen brightness completely to 0 (so it doesn't glow or waste power) while still keeping the Mac wide awake. Opening the lid brings your brightness right back!
- **Sleep Scheduler**: Set a days, hours, and minutes countdown to put your Mac to sleep. The last duration and its safety options are saved, and menu-bar mode provides a quick start action plus a live remaining-time display.

## Requirements

- macOS 13.5 or newer
- Apple Silicon or Intel Mac
- Xcode/Xcode Command Line Tools (for building from source)

## Install

Download the native bundle for your Mac from the Releases page: `Sleep-Disabler-arm64.app` for Apple Silicon or `Sleep-Disabler-x86_64.app` for Intel. Each is a separate, single-architecture app bundle. Extract it and move it to your Applications folder.

On the first launch, the app opens in Window Mode. This makes it easier to find, especially on MacBook Pro where a menu bar icon could be hidden near the camera notch.

## Permissions

Sleep Disabler uses macOS's `pmset` command to change power settings. Running its state-changing commands requires administrator permission.

The app checks for its restricted permission policy when it starts. If the policy is missing, invalid, or is the app's older broad rule, it asks whether you want to install or update it. The installation is validated with `visudo` before it atomically replaces only `/etc/sudoers.d/sleep_disabler`.

The current policy permits only the exact `pmset` mutations used by the app: enabling/disabling `disablesleep`, setting `sleep` and `displaysleep` to a bounded 0–180-minute value for AC or battery power, and `sleepnow`. It does not permit arbitrary `pmset` arguments. On sudo 1.9.10 or newer it uses anchored sudoers regular expressions; older sudo versions receive an equivalent enumerated exact-command policy.

```text
%admin ALL=(root) NOPASSWD: /usr/bin/pmset ^-a disablesleep [01]$, /usr/bin/pmset ^-a sleep 0$, /usr/bin/pmset ^-a displaysleep 0$, /usr/bin/pmset ^-[bc] (sleep|displaysleep) (0|[1-9][0-9]?|1[0-7][0-9]|180)$, /usr/bin/pmset ^sleepnow$
```

This lets the app run the required commands without asking for your password every time. If a prior app-managed broad rule exists, macOS may need to ask for administrator authorization to replace the protected sudoers file; the old `pmset` permission cannot safely grant file-write access by itself.

### Manual setup

Open Terminal and run:

```bash
sudo EDITOR=nano visudo -f /etc/sudoers.d/sleep_disabler
```

Add this exact line on systems with sudo 1.9.10 or newer. On older versions, use the app installer so it can write the equivalent enumerated policy safely:

```text
%admin ALL=(root) NOPASSWD: /usr/bin/pmset ^-a disablesleep [01]$, /usr/bin/pmset ^-a sleep 0$, /usr/bin/pmset ^-a displaysleep 0$, /usr/bin/pmset ^-[bc] (sleep|displaysleep) (0|[1-9][0-9]?|1[0-7][0-9]|180)$, /usr/bin/pmset ^sleepnow$
```

Save the file in Nano with `Control + O`, press `Enter`, then exit with `Control + X`.

Finally, set the file permissions:

```bash
sudo chmod 440 /etc/sudoers.d/sleep_disabler
```

Only add a `sudoers` rule if you understand what it does. If you want to remove it, delete `/etc/sudoers.d/sleep_disabler` using an administrator-authorized file-management method you are comfortable with.

## Building from source

1. Open `Sleep Disabler.xcodeproj` in Xcode.
2. Select the **Sleep Disabler** target.
3. Choose your Mac as the run destination.
4. Build and run with `Command + R`.

For release archives, from the project root run `zsh Scripts/archive-native.sh` (without `sudo`) as documented in [RELEASE.md](RELEASE.md). Do not combine the archives with `lipo`.

*(If you don't have the full Xcode app installed and want to build directly from Terminal using Command Line Tools, check out [compile.md](compile.md) for the full guide!)*

The deployment target is macOS 13.5+ by project default.

## How it works

When sleep is disabled, the app first reads the exact Battery and AC timer settings and saves them to `UserDefaults`. It uses `pmset -g custom` when it supplies both sources, with a structured Power Management preferences fallback for current macOS versions that omit the inactive source. If it cannot capture a complete restoration baseline, it refuses to change settings. It then runs these commands through `pmset` to keep the Mac awake:

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

For the **Battery Failsafe**, the app uses IOKit Power Sources data, including the system-level providing-power-source value, and change notifications rather than parsing `pmset` text. The feature is unavailable when no internal battery is present.

For the **Lid-Closed Dimmer**, the app reads the undocumented `AppleClamshellState` directly through IOKit and observes general IOKit/workspace power changes. Because Apple does not publish a stable lid-change API, it uses a direct IOKit fallback only while a lid-dependent feature is active; it never starts a recurring `ioreg` process. The dimmer remains visible but unavailable on Macs without a detected built-in display. Brightness control remains isolated because `DisplayServices` is private and must be hardware-tested on both native architectures.

The **Sleep Scheduler** uses the selected days, hours, and minutes as a countdown and then runs `pmset sleepnow`. Its saved values are restored at launch, so the menu-bar quick start always uses the last configured schedule. While a schedule is active, the menu displays the live remaining time. The optional closed-lid condition prevents starting while the lid is open and cancels the schedule if the lid opens. The optional sleep-disabled condition only permits a schedule while Sleep Disabler is active, and cancels it when normal sleep is restored.

## License

This project is completely open-source and free to use. You are welcome to modify the code, build on top of it, fork the repository, or integrate it into your own custom projects.

All contents of this project come with **absolutely no warranty**.

## AI disclosure

GPT-OSS in Antigravity-IDE was used to help format parts of the code.
