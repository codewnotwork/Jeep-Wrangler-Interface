# In-Jeep logger: a Raspberry Pi that records every drive

A Raspberry Pi lives in the Jeep with the OBDLink EX plugged into it. It starts
with the key, records every drive with the same recorder the app's dashboard
uses, and sends each drive to your PC over home WiFi. Drives show up in
`Documents\jeep-data` as folders named like `2026-11-20 0815 jeep drive`, with
the usual `report.md` (for you or an LLM), `report.json` and `log.csv`. The
app's **In-Jeep logger** tab lists them.

## What to buy (about $110)

| Part | About | Notes |
|---|---|---|
| Raspberry Pi **3 Model A+**, Zero 2 W or Zero W | $15–25 | Any of these works. The 3 A+ is the easiest: its pin header is already fitted, and it has a full-size USB port for the OBDLink. The Zero 2 W and the original Zero W need a 2x20 pin header (pre-fitted on the "WH" versions, or soldered or pressed in with a GPIO Hammer Header) plus the OTG adapter below. |
| Silicognition **LiFePO4wered/Pi+** | $50–60 | The power board that makes shutdown safe. Buy a listing that **includes the cell**. If you buy the cell separately it must be a **3.2 V LiFePO4** cell of the size listed (usually 18650). A normal 3.7 V lithium cell looks the same and would damage the board. |
| High-endurance microSD, 32 GB | $10 | SanDisk High Endurance or Samsung PRO Endurance. |
| Micro-USB OTG adapter (micro-USB male to USB-A female) | $6 | **Zero 2 W and Zero W only.** The OBDLink EX plugs into this, and this into the Pi's **USB** port (the inner one, not "PWR"). On the 3 A+ the OBDLink plugs straight into its USB port. |
| USB car charger (2.4 A or more) + short micro-USB cable | $12 | Plugs into the power board's USB input, **never the Pi's own power socket** (the board feeds the Pi through the pin header). Use a 12 V outlet that goes **off with the key**. |
| Small case or Velcro | $5–10 | |

Either Pi runs the same software. The 3 A+ uses more power (about 2.5 W against 1 W),
which makes no difference here: the car charger supplies it while driving, and the
battery only has to carry it for a few minutes after the key is off. It is also a bit
bigger: 65 x 56 mm.

The original **Zero W** works too, with three differences:
- In Raspberry Pi Imager choose **Raspberry Pi Zero** as the device and **Raspberry Pi OS
  Lite (32-bit)**. The 64-bit version doesn't run on it.
- It is slower. The install takes 20 to 40 minutes, and saving a long drive after the key
  is off can take a minute or two. The logger waits for that before shutting down.
- Its WiFi only uses 2.4 GHz. Most home routers offer both, so this rarely matters, but
  a 5 GHz-only network won't work.

## Why it won't corrupt the SD card

- **Power board.** The Pi runs from the board's small LiFePO4 battery while the
  car charger keeps it full. When you turn the key off, the board notices the
  charger has gone dead. The logger finishes and saves the drive. It gives the
  upload up to about 2½ minutes (less if everything is already sent, or if your PC
  isn't answering). Then it shuts the Pi down properly, and the board cuts power. When you
  start the truck, the board powers the Pi up again. The battery also rides
  through the voltage dip while cranking. LiFePO4 handles a hot parked car far
  better than ordinary lithium packs.
- **Careful writing.** The drive's log is forced onto the card every 5 seconds,
  each drive has its own folder, and settings files are replaced in one step,
  never half-written. If power were ever cut abruptly, at most a few seconds are
  lost. At the next start, the drive is finished from what reached the card, and
  its report is marked "saved after a power cut".
- **Fewer writes.** The installer moves the system's own logs into memory, turns
  off the swap file and background updates, and turns on automatic file system
  repair at start-up.

## Setting it up (once)

### 1. Prepare the SD card on your PC

1. Install **Raspberry Pi Imager** from raspberrypi.com/software and open it.
2. **Device:** Raspberry Pi 3 (for the 3 A+), Raspberry Pi Zero 2 W, or Raspberry Pi Zero (for the Zero W).
   **Operating system:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**, or the **32-bit** one for the Zero W. **Storage:** the SD card.
3. Press **Next**, then **Edit settings** (or "Customise", depending on the version):
   - Hostname: `wrangler-pi`
   - Username and password: choose any, and write them down
   - Wireless LAN: your home WiFi name and password, and your country
   - Services tab: **Enable SSH**, **Use password authentication**
4. Save, then write the card.

### 2. First start, at home

1. Put the card in the Pi. Assemble the power board and its battery on the Pi
   following the board's instructions.
2. Power it from any USB charger in the house (into the power board's input). Wait 3 minutes.

### 3. Install the logger from the app

1. Open **Wrangler Diagnostics** and go to the **In-Jeep logger** tab.
2. Pi's name: leave `wrangler-pi.local`. Fill in the user name and password from step 1.
   Power board: **LiFePO4wered/Pi+**.
3. Press **Install on Pi** and wait 5 to 15 minutes (20 to 40 on a Zero W). Progress shows on the page.
   The Pi restarts at the end.
4. Tick **Start the receiver when Windows starts**.
5. If Windows asks whether to allow Python on your network, click **Allow** (private
   networks). That is what lets the Pi reach the PC.

If the app can't find `wrangler-pi.local`, look up the Pi's address in your WiFi
router's list of devices (for example `192.168.1.57`) and type that instead.

### 4. In the Jeep

1. Plug the OBDLink EX into the truck's port and into the Pi's USB port (on a Zero, through the OTG adapter).
2. Plug the car charger into a 12 V outlet that turns off with the key. Connect it to the power board.
3. Tuck the Pi somewhere it won't get stepped on. Under or behind the dash beats the
   top of the dashboard in the sun.

That's it. While a drive is being recorded, the Pi's green light blinks like a heartbeat.

## Day to day

- Nothing to do. Drive as usual.
- Drives reach the PC when you pull in at home, while the Pi is still on its
  battery, or the next time you start the truck at home. The PC can be off or asleep: drives wait on the
  Pi (it keeps hundreds) until both are on at home.
- Trips shorter than one minute of engine running (moving the truck) aren't kept.
- The tab shows when the Pi last checked in, and how many drives are waiting.
- To update the Pi after the app updates, press **Install on Pi** again. Recorded
  drives are kept.

## How it works (for the curious, or an AI helper)

- Pi: `wrangler_obd/carlogger.py`, run by the systemd service `wrangler-logger`
  from `/opt/wrangler/src`, with settings in `/etc/wrangler-logger.json`. Drives
  are kept in `/var/lib/wrangler/drives/<pi>-<date>-<time>/`, and the folder is
  named `….partial` while recording. Logs: `journalctl -u wrangler-logger`.
- PC: `wrangler_obd/receiver.py`, a small web server on port 47800 that also
  answers "who's there?" broadcasts on UDP port 47801. Every request carries
  the pairing code stored in `Documents\jeep-data\pi-pairing.json`, and every file
  is checked with SHA-256. A drive only counts as delivered once the PC confirms it.
- Power board: `lifepo4wered-cli`. The logger sets `auto_boot` to 3 (start
  when outside power appears) and `auto_shdn_time` to 5 minutes as a backstop.
  These go in the board's memory, not its flash, so taking the battery out for
  a few minutes resets them.
- Installer: `wrangler_obd/pisetup.py` (SSH via `paramiko`, installed on first
  use) and `wrangler_obd/pi/install.sh`.
