# Deploying Power Master on Raspberry Pi 3B

## Prerequisites

- Raspberry Pi 3B with Raspberry Pi OS 64-bit Lite installed
- Pi connected to the same LAN as the FoxESS inverter and Shelly devices
- SSH access to the Pi
- A dev machine (Windows/Mac/Linux) with Docker Desktop for cross-building

---

## Step 1: Cross-build the ARM64 image (on your dev machine)

Building on the Pi 3B is slow (~20+ min). Build on your dev machine instead.

```bash
# From the project root (where Dockerfile is)
bash deploy/build-pi-image.sh
```

This produces `power-master-pi.tar` in the project root (~150-200MB).

If you prefer to build directly on the Pi instead, skip this step and do
`docker compose -f docker-compose.pi.yml build` on the Pi in Step 4.

---

## Step 2: Transfer files to the Pi

```bash
# Transfer the image tarball
scp power-master-pi.tar pi@<pi-ip>:~/

# Transfer the deployment files
scp docker-compose.pi.yml pi@<pi-ip>:~/
scp deploy/pi-setup.sh pi@<pi-ip>:~/
scp config.defaults.yaml pi@<pi-ip>:~/
```

---

## Step 3: Run the setup script on the Pi

```bash
ssh pi@<pi-ip>

# Run the setup script
bash ~/pi-setup.sh
```

This will:
- Install Docker and the docker compose plugin
- Configure 2GB swap (Pi 3B only has 1GB RAM)
- Create `/opt/power-master/data` for persistent storage
- Copy `config.defaults.yaml` as a starting config

**Log out and back in** after setup (needed for docker group permissions):
```bash
exit
ssh pi@<pi-ip>
```

---

## Step 4: Configure

Edit the config file with your settings:

```bash
nano /opt/power-master/data/config.yaml
```

Key settings to change:

| Setting | Description | Example |
|---------|-------------|---------|
| `hardware.foxess.host` | Inverter IP on LAN | `192.168.1.100` |
| `hardware.foxess.port` | Modbus TCP port | `502` |
| `providers.tariff.amber_api_key` | Amber Electric API key | `psk_...` |
| `providers.solar.latitude` | Your latitude | `-27.47` |
| `providers.solar.longitude` | Your longitude | `153.02` |
| `dashboard.auth.enabled` | Enable login | `true` |
| `dashboard.auth.admin_password` | Admin password | `your-password` |

---

## Step 5: Load the image and start

```bash
# Load the pre-built image
docker load -i ~/power-master-pi.tar

# Move compose file to a working location
cp ~/docker-compose.pi.yml /opt/power-master/

# Start the container
cd /opt/power-master
docker compose -f docker-compose.pi.yml up -d
```

---

## Step 6: Verify

```bash
# Check container is running
docker ps

# Check startup logs
docker logs -f power-master

# Check memory usage (should be under 768MB)
docker stats --no-stream power-master
```

Open the dashboard in a browser: `http://<pi-ip>:8080`

---

## Ongoing Operations

### View logs
```bash
docker logs -f power-master
```

### Restart
```bash
cd /opt/power-master
docker compose -f docker-compose.pi.yml restart
```

### Stop
```bash
cd /opt/power-master
docker compose -f docker-compose.pi.yml down
```

### Update to a new version
```bash
# On dev machine: rebuild
bash deploy/build-pi-image.sh

# Transfer to Pi
scp power-master-pi.tar pi@<pi-ip>:~/

# On Pi: load new image and restart
ssh pi@<pi-ip>
docker load -i ~/power-master-pi.tar
cd /opt/power-master
docker compose -f docker-compose.pi.yml up -d
```

### Backup database
```bash
cp /opt/power-master/data/power_master.db ~/power_master_backup_$(date +%Y%m%d).db
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Container exits immediately | Check logs: `docker logs power-master` |
| Can't reach inverter | Verify Pi is on same LAN, ping inverter IP |
| Out of memory | Check `docker stats`, ensure swap is active: `free -h` |
| Slow startup | Normal on Pi 3B — allow 30-60s for solver first run |
| Permission denied on docker | Run `sudo usermod -aG docker $USER` then log out/in |
| Image won't load | Ensure image was built for `linux/arm64` |

---

## Hardware Notes

- **Pi 3B (1GB RAM):** Container limited to 768MB, 2GB swap configured
- **SD card wear:** SQLite writes are fsync'd (`synchronous=FULL`). For heavy use, consider an external USB drive mounted at `/opt/power-master/data`
- **Network:** Uses host networking for direct Modbus TCP access to inverter on LAN
