# BETTE-R-LED-ARMADA-OS
A Better LED Controller alternative with game profiles and LED management control features

A [Decky Loader](https://decky.xyz) plugin for Armada OS (tested on the Retroid Pocket Nova) that takes over the analog-stick RGB LEDs.

## Features
- **LEDs** master switch and **Home color** for the home screen (and games without their own color)
- **LED Rain Mode**: white raindrops on the home screen
- **Breathe while charging**: orange breath while plugged in, red below 10%; a white dance when unplugged
- **Per-game LED colors** with color and brightness, saved automatically for the running game
- **LED React**: the sticks pulse and flash with the game's sound, each stick following its own side
- **Ambilight**: each stick follows the main color on its half of the screen
- Lights stay dark while the device sleeps (or show the charging color while plugged in)

Leave Armada Control's own LEDs switched off; BETT-R LED drives the lights itself.

## Install
Download `bettr-led.zip` from the latest [release](../../releases) and install it from Decky's developer settings ("Install plugin from ZIP").

## Build
```bash
pnpm install
pnpm run build
```

Made by ILLMENTALITYAPPS.
