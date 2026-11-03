# RaspberryPi bluetooth reciever and song viewer

This is a "simple" project that captures song info from incoming bluez and bluetoothctl streams and displays it on a nice local web page

## Requirements:
- A RaspberryPi running desktop RaspberryPiOS Trixie
- An internet connection
- A Bluetooth connection (using bluez)
- a discogs personal access token

## Setup:
- run:
- sudo apt update
- sudo apt install bluez bluez-tools libspa-0.2-bluetooth python3-venv
- in your ~/ directory make a folder called hifi-bt-sys
- put the app.py and index.html in the directory
- in hifi-bt-sys run:
- python3 -m venv venv
- venv/bin/pip install dbus-fast aiohttp
- go back to your home folder and make sure your bt-a2dp-fix.py file is there
- copy it over
- sudo cp bt-a2dp-fix.py /usr/local/bin/
- same for the service
- sudo cp bt-a2dp-fix.service /etc/systemd/system/
- this will fix the bluetooth connectivity issues
- put main.conf in /etc/bluetooth/main.conf
- sudo systemctl restart bluetooth
- same for /etc/systemd/system/bt-agent.service
- sudo systemctl enable --now bt-agent
- check what id the hdmi port is on:
- wpctl status
- Find the HDMI sink in the Sinks list
- wpctl set-default <ID>
- Create ~/.config/labwc/autostart
- "$HOME/hifi-bt-sys/venv/bin/python" "$HOME/hifi-bt-sys/app.py" &
sh -c 'sleep 5; chromium --kiosk --noerrdialogs --disable-infobars http://localhost:8080' &
- put your discogs personal access token at the top of app.py where it says DISCOGS_TOKEN
- save
- sudo reboot

should all work now
