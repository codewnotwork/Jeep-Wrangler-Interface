// Wrangler in-Jeep OBD logger case
// Raspberry Pi 4 Model B + Silicognition LiFePO4wered/Pi+ (18650 cell)
//
// Render one part at a time:
//   openscad -D 'part="base"' -o base.stl wrangler-logger-case.scad
//   openscad -D 'part="lid"'  -o lid.stl  wrangler-logger-case.scad
//   part="assembly" shows both parts plus ghost boards (preview only)
//
// Coordinates: origin = bottom-left corner of the Pi PCB (SD card end,
// front edge with the HDMI/USB-C ports). +X runs toward the USB/Ethernet
// end, +Y toward the 40-pin header. Z = 0 is the top of the case floor.

part = "assembly";          // "base" | "lid" | "assembly" | "print"

/* ---------------- printing ---------------- */
wall      = 2.0;   // side walls
floor_t   = 2.0;   // base floor
lid_t     = 2.0;   // lid plate
fit       = 0.3;   // lid lip clearance (raise to 0.4 if your printer runs fat)
$fn = 40;

/* ---------------- Raspberry Pi 4 (official drawing) ---------------- */
pi_l = 85; pi_w = 56; pi_t = 1.5;
pi_holes = [[3.5,3.5],[61.5,3.5],[3.5,52.5],[61.5,52.5]];
standoff_h = 10;           // floor to Pi PCB bottom (18650 hangs 7.5 mm below)
pi_z = standoff_h;         // Pi PCB bottom
pi_top = pi_z + pi_t;

// Ports on the +X end: [center Y, width, height above PCB top]
usb2 = [9.0,  14.6, 16.0];
usb3 = [27.0, 14.6, 16.0];
eth  = [45.75,16.2, 13.6];
port_clr = 0.8;            // all-round clearance around each port

// microSD (under the PCB at the -X end)
sd_y = 28;

/* ---------------- LiFePO4wered/Pi+ (rev 8, 18650) ---------------- */
// Pi-Zero-sized board over the GPIO header, battery strip beyond the
// Pi's header edge. 77.5 x 52 mm. Top of board ~14 mm above Pi PCB bottom.
pp_x0 = 0;  pp_x1 = 77.5;      // extends toward the USB end (assumed)
pp_y0 = 26; pp_y1 = 78;
pp_top = pi_z + 14;
pp_bot = pp_top - 1.6;
batt_bot = pi_z - 7.5;         // 18650 bottom
batt_d = 18.6;

// Opening in the SD-end wall at power-board level for the board's
// USB-C input, side-facing on/off button and LEDs.
// CONFIRM against the real board before printing (see README).
pp_win_y0 = 30; pp_win_y1 = 78;
pp_win_z0 = pi_top + 1;        // just above the Pi PCB

/* ---------------- enclosure size ---------------- */
in_x0 = -2.0;  in_x1 = pi_l + 0.3;      // ports sit flush with outside wall
in_y0 = -1.5;  in_y1 = pp_y1 + 1.5;
headroom = 4;                           // above power board for LEDs/button
in_h = pp_top + headroom;               // interior height (floor top -> lid)

out_x0 = in_x0 - wall; out_x1 = in_x1 + wall;
out_y0 = in_y0 - wall; out_y1 = in_y1 + wall;
corner_r = 3;

/* ---------------- mounting ---------------- */
flange_len = 12;     // screw / zip-tie flanges on both ends
flange_w   = 34;
flange_t   = 3;
slot_d     = 4.6;    // fits #8 or M4 screws, or 4.8 mm zip ties
slot_len   = 7;

// Lid screw ears on the long sides: M3 (self-tapping into base)
ear_x = [12, 72];
ear_w = 10; ear_d = 7; ear_h = 8;

/* ================================================================ */
module rrect(x0,y0,x1,y1,h,r=corner_r){
    hull() for (x=[x0+r,x1-r], y=[y0+r,y1-r])
        translate([x,y,0]) cylinder(r=r,h=h);
}

module slot2d(len,d){ hull(){ translate([-len/2+d/2,0]) circle(d=d);
                             translate([ len/2-d/2,0]) circle(d=d);} }

module base(){
    difference(){
        union(){
            // shell
            translate([0,0,-floor_t]) rrect(out_x0,out_y0,out_x1,out_y1,in_h+floor_t);
            // end flanges
            for (side=[0,1]){
                fx = side==0 ? out_x0-flange_len : out_x1;
                cy = (out_y0+out_y1)/2;
                translate([fx - (side==0?0:corner_r), cy-flange_w/2, -floor_t])
                    rrect(0,0,flange_len+corner_r,flange_w,flange_t,2);
            }
            // lid screw ears
            for (x=ear_x, s=[0,1]){
                y = s==0 ? out_y0-ear_d : out_y1;
                translate([x-ear_w/2, y, in_h-ear_h]) hull(){
                    cube([ear_w,ear_d,ear_h]);
                    // gusset down to the wall
                    translate([0, s==0?ear_d:0, -ear_d]) cube([ear_w,0.01,0.01]);
                }
            }
        }
        // cavity
        rrect(in_x0,in_y0,in_x1,in_y1,in_h+1,1.2);

        // flange slots
        for (side=[0,1]){
            fx = side==0 ? out_x0-flange_len/2-1 : out_x1+flange_len/2+1;
            for (dy=[-flange_w/2+7, flange_w/2-7])
                translate([fx,(out_y0+out_y1)/2+dy,-floor_t-1])
                    linear_extrude(flange_t+2) rotate(90) slot2d(slot_len,slot_d);
        }
        // ear pilot holes (M3 self-tap)
        for (x=ear_x, s=[0,1]){
            y = s==0 ? out_y0-ear_d/2 : out_y1+ear_d/2;
            translate([x,y,in_h-ear_h-1]) cylinder(d=2.6,h=ear_h+2);
        }

        // USB / Ethernet end
        for (p=[usb2,usb3,eth])
            translate([in_x1-1, p[0]-p[1]/2-port_clr, pi_top-0.6])
                cube([wall+2, p[1]+2*port_clr, p[2]+port_clr+0.6+1.0]);

        // microSD slot with finger notch
        translate([out_x0-1, sd_y-9, pi_z-6]) cube([wall+2, 18, 7.2]);

        // power-board window (USB-C in, button, LEDs, Pi LEDs) - open to top
        translate([out_x0-1, pp_win_y0, pp_win_z0])
            cube([wall+2, pp_win_y1-pp_win_y0, in_h]);

        // vents: front wall (above the Pi's HDMI/USB-C) and back wall
        for (i=[0:8]){
            vx = 8 + i*8.2;
            translate([vx, out_y0-1, pi_top+5]) cube([3.2, wall+2, in_h-pi_top-8]);
            translate([vx, in_y1-1, 4])          cube([3.2, wall+2, in_h-8]);
        }
        // floor vents under the Pi's USB controller / PMIC area
        for (i=[0:4]) translate([40+i*7, 6, -floor_t-1]) cube([3, 18, floor_t+2]);
    }

    // standoffs
    for (h=pi_holes){
        gpio_side = h[1] > 30;
        translate([h[0],h[1],-0.01]) difference(){
            cylinder(d= gpio_side ? 7.5 : 6, h=standoff_h);
            if (gpio_side){
                // nut from the power-board screw sits in this pocket
                translate([0,0,standoff_h-2.6]) cylinder(d=6.0, h=3, $fn=6);
                translate([0,0,1]) cylinder(d=3.0, h=standoff_h);
            } else {
                translate([0,0,1]) cylinder(d=2.2, h=standoff_h); // M2.5 self-tap
            }
        }
    }
}

module lid(){
    difference(){
        union(){
            rrect(out_x0,out_y0,out_x1,out_y1,lid_t);
            // locating lip that drops inside the walls
            translate([0,0,-3]) difference(){
                rrect(in_x0+fit,in_y0+fit,in_x1-fit,in_y1-fit,3,1);
                translate([0,0,-1]) rrect(in_x0+fit+1.4,in_y0+fit+1.4,in_x1-fit-1.4,in_y1-fit-1.4,5,0.5);
                // keep lip out of the power-board window and port openings
                translate([in_x0-1, pp_win_y0, -1]) cube([4, pp_win_y1-pp_win_y0, 5]);
                // USB/Ethernet tops reach almost to the lid at the +X end
                translate([in_x1-4, in_y0-1, -1]) cube([6, eth[0]+eth[1]/2+port_clr+2-in_y0, 5]);
            }
            // ears
            for (x=ear_x, s=[0,1]){
                y = s==0 ? out_y0-ear_d : out_y1;
                translate([x-ear_w/2, y, 0]) cube([ear_w,ear_d,lid_t+0.5]);
            }
        }
        for (x=ear_x, s=[0,1]){
            y = s==0 ? out_y0-ear_d/2 : out_y1+ear_d/2;
            translate([x,y,-1]) cylinder(d=3.4,h=5);
        }
        // vent slots over the whole top (LEDs show through these too)
        for (i=[0:13]) translate([3 + i*5.6, 4, -1]) cube([2.6, 28, lid_t+2]);
        for (i=[0:13]) translate([3 + i*5.6, 40, -1]) cube([2.6, 34, lid_t+2]);
        // label
        translate([(out_x0+out_x1)/2, 36.5, lid_t-0.6])
            linear_extrude(1) text("JEEP OBD LOGGER", size=3.6, halign="center",
                                   valign="center", font="Liberation Sans:style=Bold");
    }
}

/* ---------- ghost boards for checking fit ---------- */
module boards(){
    color("green",0.85) translate([0,0,pi_z]) cube([pi_l,pi_w,pi_t]);
    color("silver") for (p=[usb2,usb3]) translate([pi_l-17.4+2.2, p[0]-p[1]/2+0.4, pi_top]) cube([17.4,13.8,15.6]);
    color("silver") translate([pi_l-21+2.5, eth[0]-8, pi_top]) cube([21,16,13.5]);
    color("dimgray") translate([-2.5, sd_y-5.5, pi_z-1.4]) cube([15,11,1.2]);
    color("black") translate([27,50,pi_top]) cube([51,5,8.5]);           // header
    color("royalblue",0.85) translate([pp_x0,pp_y0,pp_bot]) cube([pp_x1-pp_x0,pp_y1-pp_y0,1.6]);
    color("orange") translate([6, pp_y1-batt_d/2-2, batt_bot+batt_d/2])
        rotate([0,90,0]) cylinder(d=batt_d,h=65);
}

if (part=="base") base();
else if (part=="lid") lid();
else if (part=="print"){
    base();
    translate([0, out_y1-out_y0+25, lid_t]) mirror([0,0,1]) lid();
}
else {
    base();
    boards();
    translate([0,0,in_h+0.01]) color("lightgray",0.55) lid();
}
