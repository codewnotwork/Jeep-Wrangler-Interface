# In-Jeep logger case

A two-piece 3D-printable case for the in-Jeep OBD logger:
Raspberry Pi 4 Model B + Silicognition LiFePO4wered/Pi+ (18650 cell) on top,
with the OBDLink EX plugged into one of the Pi's USB-A ports.

| File | What it is |
|---|---|
| `base.stl` | Tub that holds the Pi. Prints upright, no supports. |
| `lid.stl` | Vented lid. Print it top-face down (lip pointing up), no supports. |
| `wrangler-logger-case.scad` | OpenSCAD source. Every size is a named setting at the top. |
| `preview-*.png` | Renders of the assembly, the print layout, and both ends. |

Outside size: about 92 x 99 x 32 mm (115 mm long including the mounting flanges).

## Material

Use **ASA** (best) or **PETG**. A closed Jeep in the sun gets well past the
point where PLA softens (about 55 °C), and the case would sag or warp.
PETG is fine under a seat or in a console out of direct sun. ASA holds up
on or near the dash. Print with 3 walls, 20 % infill, 0.2 mm layers.

## Hardware

* 2 x M2.5 x 6 mm screws (self-tapping, or regular screws into the 2.2 mm holes) for the
  two Pi holes on the side away from the GPIO header.
* The two holes next to the GPIO header already carry the power board's own
  M2.5 screws and nuts. Those nuts drop into the hex pockets on top of the
  two wider standoffs, so no extra screws there.
* 4 x M3 x 8 mm self-tapping screws to hold the lid on (through the four ears).
* Mounting: either Velcro / 3M Dual Lock on the flat bottom, or #8 / M4
  screws or zip ties through the slots in the two end flanges.

## Heat sinks

The power board sits about 9-10 mm above the Pi's CPU and RAM. Put the
**short** heat sinks (6 mm tall or less) on the CPU and RAM, which sit under
the power board. The USB controller chip near the USB ports has open space
above it, so any heat sink fits there.

## Assembly

1. Stack the power board on the Pi with its screws, spacers and nuts as its
   instructions say. Fit the heat sinks first.
2. Take the microSD card out.
3. Lower the stack into the base, USB/Ethernet end first, so the ports sit in
   their openings, then let the SD end down onto the standoffs. The nuts
   under the Pi drop into the two hex pockets.
4. Put the two short screws into the other two standoffs. Refit the SD card
   through the slot at the end.
5. Plug the car charger cable into the power board's USB-C input through the
   window at the SD end, plug in the OBDLink EX, and screw the lid on.

## Power cable

Revision 8 and later of the LiFePO4wered/Pi+ have a **USB Type-C** power
input, so a **USB-A to USB-C** cord from the car charger is the right cable.
Plug it into the power board, not the Pi's own USB-C port. (Older boards,
revision 7 and earlier, used Micro-USB; if yours has a Micro-USB socket
you'd need a USB-A to Micro-USB cord instead.)

## Before you print the base: check one thing

The Pi 4 dimensions come from the official Raspberry Pi drawing, so the
Pi's ports, SD slot and mounting holes are exact. The power board's
published specs give its size (77.5 x 52 mm), its height above the Pi
(14 mm) and how far the 18650 hangs below the Pi (7.5 mm), but not where
the USB-C socket, button and LEDs sit along its edges.

The case assumes the battery runs along the Pi's GPIO side and the board
extends toward the Pi's USB end, with the USB-C input and button on the
SD-card end. That end has a wide open window at power-board level so the
cable, button and LEDs are all reachable. Once the board arrives, check
that the USB-C socket is on that end. If it's somewhere else, change
`pp_win_*` (or ask Claude to) and re-export.

## Changing things

Open `wrangler-logger-case.scad` in OpenSCAD (free, openscad.org), change a
setting at the top, then press F6 and File > Export > STL. Or from a
command line:

```
openscad -D 'part="base"' -o base.stl wrangler-logger-case.scad
openscad -D 'part="lid"'  -o lid.stl  wrangler-logger-case.scad
```

Useful settings: `fit` (lid looseness), `standoff_h`, `headroom`,
`flange_len` / `slot_d` (mounting), and `pp_win_y0` / `pp_win_y1` /
`pp_win_z0` (power-board window).
