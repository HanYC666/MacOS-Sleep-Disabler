# 🌙 Sleep Disabler for macOS

A ultra-lightweight, native macOS utility built in SwiftUI that prevents your Mac from sleeping or turning off its display. It provides a seamless transition between standard Window Mode and a streamlined Menu Bar app, putting total control over your system's power management right at your fingertips.

---

## ✨ Features

* **Anti-Notch First Launch:** By default, the app launches in **Window Mode** on its very first run. This ensures the app is immediately visible and prevents the menu bar icon from getting trapped behind the camera notch on modern MacBook Pro models.
* **Persistent Memory:** Seamlessly switch between Window Mode and Menu Bar Mode. The app remembers your exact layout and sleep preferences across restarts.
* **Launch at Login:** Option to automatically start the utility when you boot up your Mac.
* **Native Power Management:** Safely hooks directly into macOS's native `pmset` architecture.

---

## 📊 Performance & Footprint

This application is engineered for maximum efficiency. It runs silently in the background with practically zero impact on system resources.

| Metric | Resource Usage |
| --- | --- |
| 💾 **Disk Space** | 394 KB (Total compiled app size) |
| 🧠 **Memory (RAM)** | ~19 MB when idle |
| ⚡ **CPU Usage** | 0% when idle |

---

## 🚀 Setup Guide (For MacOS 13.5+)

Setting up the app on a fresh installation of macOS is incredibly straightforward. Follow these steps to get up and running:

### Step 1: Install the Application

1. Download the compiled `Sleep Disabler.app` from the Releases page (or build it directly from this source code using Xcode).
2. Drag and drop `Sleep Disabler.app` into your **Applications** folder.
3. Open the app. Because it defaults to **Window Mode**, it will appear cleanly in the center of your screen.

### Step 2: Configure System Permissions (`pmset`)

To toggle system sleep states without requiring you to type your administrator password every single time, the app needs permission to run the macOS power management tool (`/usr/bin/pmset`).

#### Method A: Automatic Setup (Recommended)

When you first click "Disable Sleep", the app will detect if it lacks permissions and prompt you with an alert asking to install the configuration. Clicking **Enable** will automatically authorize it.

#### Method B: Manual Setup via Terminal (Using Nano)

If you prefer to configure the security rules manually, or if your system environment restricts automatic deployment, you can use the terminal.

> ⚠️ **Important Note:** Running standard `sudo visudo` opens the configuration file inside **Vim**, which can be highly confusing to exit if you aren't familiar with it. Follow the command below to force macOS to open it in **Nano** instead:

1. Open your terminal app and paste the following command to edit the security file safely using the Nano text editor:

```bash
sudo EDITOR=nano visudo /etc/sudoers.d/sleep_disabler

```

2. Enter your Mac's login password when prompted.
3. A interactive text editor will appear. Copy and paste the exact line below into the last line (assuming your user account is an admin account, if not replace '%admin' with your username):

```text
%admin ALL=(ALL) NOPASSWD: /usr/bin/pmset

```

4. Save and exit **Nano**:
* Press `Control + O` then press `Enter` to write the file.
* Press `Control + X` to exit the editor.


5. Secure the file permissions by running this final command:

```bash
sudo chmod 440 /etc/sudoers.d/sleep_disabler

```

Your app is now fully configured and ready to roll!

---

## 🛠️ How It Works Under the Hood

The application dynamically coordinates your preferences through two core macOS systems:

* `UserDefaults`: Saves your chosen interface configuration (Window vs. Menu Bar) locally in `~/Library/Preferences/`. This is completely external to the application binary, meaning you can share the `.app` bundle with a friend, and it will still boot freshly in Window Mode on *their* machine without bringing your custom preferences along.
* `pmset`: Executes low-overhead commands to toggle `disablesleep`, `sleep`, and `displaysleep` modes instantly.

---

## 📄 License & Open Source Terms

This project is completely **open-source** and free to use. You are welcome to modify the code, build on top of it, fork the repository, or integrate it into your own custom projects.

All contents of this project come with **absolutely no warranty**.

### ⚠️ Condition of Use

You are free to distribute, remix, and adapt this software, but **you must credit the original author (me HanYC666)** in your repository, documentation, or application credits.