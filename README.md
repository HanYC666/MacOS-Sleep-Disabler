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

## Requirements

- macOS 13.5 or newer
- Apple Silicone Mac (M1 chip or newer)
- Xcode, if building from source

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

The deployment target is macOS 13.5 by project default.

## How it works

When sleep is disabled, the app runs these commands through `pmset`:

```bash
sudo pmset -a disablesleep 1
sudo pmset -a sleep 0
sudo pmset -a displaysleep 0
```

When sleep is enabled again, it restores the app's default values:

```bash
sudo pmset -a disablesleep 0
sudo pmset -a sleep 10
sudo pmset -a displaysleep 10
```

The selected interface mode is stored in `UserDefaults`, so the app can remember whether it should start in Window Mode or Menu Bar Mode.

## License

This project is completely open-source and free to use. You are welcome to modify the code, build on top of it, fork the repository, or integrate it into your own custom projects.

All contents of this project come with **absolutely no warranty**.

## AI disclosure

GPT-OSS in Antigravity-IDE was used to help format parts of the code.
