# BioTime Connect Pro (Odoo 19)

Install: copy `biotime_connect_pro` to your addons path, update the app list, install.
Setup: BioTime > Configuration > Servers > create, choose auth type (JWT for 8.x/Cloud, Token for 9.x),
Test Connection, Sync Devices, Pull Employees (or set Employee > BioTime tab > BioTime Code), then
Pull Transactions. Crons then keep everything in sync.

Crons: schedule syncs (5 min), run jobs (1 min), transfer punches (10 min).
Dependencies: hr, hr_attendance, mail; Python: requests, pytz. No queue_job needed.
