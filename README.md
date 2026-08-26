# Sleep Disabler

The most lightweight macOS app that lets you quickly stop your Mac from sleeping or turning off its display.

It is written in SwiftUI and can run either as a normal window or as a menu bar app. It is useful for long-running downloads, scripts, builds, or agentic tasks where the Mac needs to stay awake.

## What it does

- Turn sleep and display sleep on or off.
- Run from a regular window or from the menu bar.
- Remember your selected mode after restarting the app.
- Optionally launch when you log in.
- Show the current state with a menu bar icon.
- Check the power settings regularly so the displayed state stays up to date.
- **Dynamic State Saving**: Remembers exactly what your Mac's sleep settings were before disabling sleep, and perfectly restores them when re-enabled.
- **Battery Failsafe**: Let's you set a custom battery percentage (like 5%). If the battery drops below this while sleep is disabled, the app saves your Mac by forcing it to sleep and dropping a notification window.
- **Lid-Closed Dimmer**: When sleep is disabled, you can close your MacBook lid to drop the screen brightness completely to 0 (so it doesn't glow or waste power) while still keeping the Mac wide awake. Opening the lid brings your brightness right back!

## Requirements

- macOS 13.5 or newer
- Apple Silicon Mac (M1 chip or newer)
- Xcode/Xcode Command Line Tools (for building from source)

## Install

Download the latest `Sleep Disabler.zip` from the Releases page, then extract it and move it to your Applications folder.

On the first launch, the app opens in Window Mode. This makes it easier to find, especially on MacBook Pro where a menu bar icon could be hidden near the camera notch.

## Permissions

Sleep Disabler uses macOS's `pmset` command to change power settings. Running `pmset` requires administrator permission.

The app checks for a password-free permission rule when it starts. If the rule is missing, it asks whether you want to install the rule. Choosing **Enable** creates `/etc/sudoers.d/sleep_disabler` with:

```text
%admin ALL=(ALL) NOPASSWD: /usr/bin/pmset
```

This lets the app run the required `pmset` commands without asking for your password every time. If you do not want the app to install the rule automatically, choose **Cancel** and set it up manually as below.

### Manual setup

Open Terminal and run:

```bash
sudo EDITOR=nano visudo -f /etc/sudoers.d/sleep_disabler
```

Add this line:

```text
%admin ALL=(ALL) NOPASSWD: /usr/bin/pmset
```

Save the file in Nano with `Control + O`, press `Enter`, then exit with `Control + X`.

Finally, set the file permissions:

```bash
sudo chmod 440 /etc/sudoers.d/sleep_disabler
```

Only add a `sudoers` rule if you understand what it does. If you want to remove it, delete `/etc/sudoers.d/sleep_disabler` using a method you are comfortable with that has administrator access, e.g. the rm command with sudo.

## Building from source

1. Open `Sleep Disabler.xcodeproj` in Xcode.
2. Select the **Sleep Disabler** target.
3. Choose your Mac as the run destination.
4. Build and run with `Command + R`.

*(If you don't have the full Xcode app installed and want to build directly from Terminal using Command Line Tools, check out [compile.md](compile.md) for the full guide!)*

The deployment target is macOS 13.5 by project default.

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

For the **Lid-Closed Dimmer**, the app polls your MacBook's `AppleClamshellState` using `ioreg`. When it detects the lid is closed, it hooks directly into macOS's private `DisplayServices` framework (which is why it needs Apple Silicon) to drop the brightness exactly to 0 without actually triggering a system sleep event! 

## License

This project is completely open-source and free to use. You are welcome to modify the code, build on top of it, fork the repository, or integrate it into your own custom projects.

All contents of this project come with **absolutely no warranty**.

## AI disclosure

GPT-OSS in Antigravity-IDE was used to help format parts of the code.
