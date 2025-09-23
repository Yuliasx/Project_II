; G-code program to cut out the letter "L"
; Uses 2 straight lines and 2 arcs as required
; Created: 2025-09-23

G21 ; Set units to mm
G90 ; Absolute positioning
G17 ; XY plane selection
G54 ; Use work coordinate system 1

; Setup parameters
F200 ; Set feed rate to 200mm/min
M3 S10000 ; Start spindle at 10000 RPM

; Move to starting position
G0 Z10 ; Raise tool to safe height
G0 X10 Y60 ; Move to start position
G0 Z2 ; Move down to just above work

; Start cutting - First straight line (vertical part of L)
G1 Z-1 F100 ; Plunge to cutting depth
G1 Y10 F200 ; Cut vertical line down

; First arc - bottom left corner with radius
G2 X15 Y5 R5 ; Counterclockwise arc with 5mm radius

; Second straight line (horizontal part of L)
G1 X50 Y5 ; Cut horizontal line to the right

; Second arc - decorative flourish at end of L
G3 X55 Y10 R5 ; Clockwise arc with 5mm radius

; Finish
G0 Z10 ; Raise tool to safe height
G0 X0 Y0 ; Return to home position
M5 ; Stop spindle
M30 ; End program
