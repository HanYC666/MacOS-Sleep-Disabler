# Compile Guide

This guide shows you how to build the Sleep Disabler app from the source code. You can do this the easy way using the Xcode interface, or the completely manual hacker way using just the raw Command Line Tools.

## Method 1: Using Xcode (The Easy Way)

1. Double-click on `Sleep Disabler.xcodeproj` to open the project in Xcode.
2. At the very top of the window, make sure your Mac is selected as the run destination.
3. Hit `Command + R` to build and run the app. It will compile everything and launch automatically.
4. **To get the actual `.app` file:** Go to the top menu bar and click `Product` -> `Archive`. Once it finishes, an organizer window will pop up. Click `Distribute App` and follow the prompts to export the `Sleep Disabler.app` file to your Mac.

## Method 2: Using the Terminal (Raw Command Line Tools Only)

If you don't even have the Xcode app installed and only have the lightweight Command Line Tools on your system, `xcodebuild` will fail. But you can still compile the app manually by calling the Swift compiler (`swiftc`) and building the `.app` bundle structure yourself!

1. Open Terminal and navigate into the main project folder:

```bash
cd "path/to/Sleep Disabler"
```

2. Run the following block of commands. This creates the app folders, compiles the Swift code (linking the required `DisplayServices` framework for the screen dimmer), and copies over the app information property list:

```bash
mkdir -p "Sleep Disabler.app/Contents/MacOS"
mkdir -p "Sleep Disabler.app/Contents/Resources"
swiftc "Sleep Disabler/"*.swift -F /System/Library/PrivateFrameworks -framework DisplayServices -o "Sleep Disabler.app/Contents/MacOS/Sleep Disabler"
cp "Sleep-Disabler-Info.plist" "Sleep Disabler.app/Contents/Info.plist"
codesign --force --deep --sign - "Sleep Disabler.app"
```

3. If there are no errors, you're done! You'll see a fully functional `Sleep Disabler.app` right there in your project folder. You can launch it by double-clicking it in Finder or running:

```bash
open "Sleep Disabler.app"
```
