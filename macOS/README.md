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
- Check the power settings regularly so the displayed state stays up to date.
- Read battery percentage and the active power source through IOKit instead of parsing `pmset` text.
- **Dynamic State Saving**: Remembers exactly what your Mac's sleep settings were before disabling sleep, and perfectly restores them when re-enabled.
- **Battery Failsafe**: Let's you set a custom battery percentage (like 5%). If the battery drops below this while sleep is disabled, the app saves your Mac by forcing it to sleep and dropping a notification window.
- **Lid-Closed Dimmer**: When sleep is disabled, you can close your MacBook lid to drop the screen brightness completely to 0 (so it doesn't glow or waste power) while still keeping the Mac wide awake. Opening the lid brings your brightness right back!
- **Sleep Scheduler**: Set a days, hours, and minutes countdown to put your Mac to sleep. The last duration and its safety options are saved, and menu-bar mode provides a quick start action plus a live remaining-time display.

## Requirements

- macOS 13.5 or newer
- Apple Silicon or Intel Mac
- Xcode Command Line Tools (for building from source)

## Install

Download `Sleep-Disabler-arm64.zip` for Apple Silicon or `Sleep-Disabler-x86_64.zip` for Intel. Each archive contains a separate native `.app`; neither is a universal binary.

On the first launch, the app opens in Window Mode. This makes it easier to find, especially on MacBook Pro where a menu bar icon could be hidden near the camera notch.

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

From the project root, run `zsh Scripts/archive-native.sh` without `sudo`. The script uses only Xcode Command Line Tools and creates separate Apple Silicon and Intel apps. See [RELEASE.md](RELEASE.md) and [compile.md](compile.md).

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

The **Sleep Scheduler** uses the selected days, hours, and minutes as a countdown and then runs `pmset sleepnow`. Its saved values are restored at launch, so the menu-bar quick start always uses the last configured schedule. While a schedule is active, the menu displays the live remaining time. The optional closed-lid condition prevents starting while the lid is open and cancels the schedule if the lid opens. The optional sleep-disabled condition only permits a schedule while Sleep Disabler is active, and cancels it when normal sleep is restored.

## License

This project is completely open-source and free to use. You are welcome to modify the code, build on top of it, fork the repository, or integrate it into your own custom projects.

All contents of this project come with **absolutely no warranty**.

## AI disclosure

GPT-OSS in Antigravity-IDE was used to help format parts of the code.
