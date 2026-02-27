# Deploying Power Master on Synology NAS

## Prerequisites

- Synology NAS with Docker package installed (DSM 7.x)
- SSH access enabled on the NAS (Control Panel > Terminal & SNMP)
- NAS on the same LAN as the FoxESS inverter and Shelly devices
- A machine with SSH/SCP access to the NAS

---

## Step 1: Transfer the project to the NAS

```bash
# From your dev machine, copy the project to the NAS
scp -r /path/to/power-master admin@<nas-ip>:~/power-master
```

Or use Synology File Station to upload the project folder to a shared folder
(e.g., `/volume1/docker/power-master`).

---

## Step 2: SSH into the NAS

```bash
ssh admin@<nas-ip>
cd ~/power-master    # or wherever you placed the project
```

---

## Step 3: Build the Docker image

```bash
# Build the image (Synology NAS is x86_64, this takes 2-5 minutes)
sudo docker compose build
```

This uses `docker-compose.yml` which tags the image as `power-master:2026-02-24-1`.

To use a custom tag:
```bash
sudo docker build -t power-master:latest .
```

---

## Step 4: Configure

Create your config file in a persistent location:

```bash
# Create a directory for persistent data (if using bind mount)
sudo mkdir -p /volume1/docker/power-master-data

# Copy the default config as a starting point
cp config.defaults.yaml /volume1/docker/power-master-data/config.yaml

# Edit with your settings
vi /volume1/docker/power-master-data/config.yaml
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

## Step 5: Start the container

### Option A: Using docker compose (recommended)

The default `docker-compose.yml` uses a named Docker volume:

```bash
sudo docker compose up -d
```

### Option B: Using a bind mount (easier to backup)

If you prefer the data on a visible shared folder:

```bash
sudo docker run -d \
  --name power-master \
  --restart unless-stopped \
  --network host \
  -e TZ=Australia/Brisbane \
  -v /volume1/docker/power-master-data:/data \
  power-master:2026-02-24-1
```

---

## Step 6: Verify

```bash
# Check container is running
sudo docker ps

# Check startup logs
sudo docker logs -f power-master

# Quick health check
curl http://localhost:8080
```

Open the dashboard in a browser: `http://<nas-ip>:8080`

You should see in the logs:
- `Connected to Fox-ESS KH at ...`
- `Initial telemetry: SOC=XX%`
- `Solver complete: status=Optimal`
- `Control loop starting (interval: 300s)`
- `Command refresh loop starting (interval: 20s)`

---

## Synology Docker GUI (Alternative)

If you prefer the DSM web interface over SSH:

1. Open **Container Manager** (or **Docker** on older DSM)
2. Go to **Image** > **Build** > select the project folder
3. Or import a pre-built image via **Image** > **Add** > **Add from file**
4. Go to **Container** > **Create**
5. Set:
   - Image: `power-master:2026-02-24-1`
   - Network: `Use same network as Docker host`
   - Volume: Map a local folder to `/data`
   - Environment: `TZ=Australia/Brisbane`
   - Restart policy: `Unless stopped`
6. Apply and start

---

## Ongoing Operations

### View logs
```bash
sudo docker logs -f power-master
```

Or via Container Manager web UI > select container > **Log** tab.

### Restart
```bash
sudo docker compose restart
```

### Stop
```bash
sudo docker compose down
```

### Update to a new version
```bash
# Transfer updated project files to NAS
scp -r /path/to/power-master admin@<nas-ip>:~/power-master

# SSH in and rebuild
ssh admin@<nas-ip>
cd ~/power-master
sudo docker compose build
sudo docker compose up -d
```

### Backup database

With named volume:
```bash
sudo docker cp power-master:/data/power_master.db ~/power_master_backup_$(date +%Y%m%d).db
```

With bind mount:
```bash
cp /volume1/docker/power-master-data/power_master.db ~/power_master_backup_$(date +%Y%m%d).db
```

### Backup config
```bash
sudo docker cp power-master:/data/config.yaml ~/config_backup.yaml
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Container exits immediately | Check logs: `sudo docker logs power-master` |
| Can't reach inverter | Verify NAS is on same LAN subnet, check `hardware.foxess.host` in config |
| Port 8080 in use | Change port mapping or stop conflicting service |
| Database corruption | Container auto-recovers on startup (integrity check + row-level recovery) |
| Permission denied | Use `sudo` for all docker commands on Synology |
| Container not restarting after DSM update | Re-enable Docker package, then `sudo docker compose up -d` |
| Force charge stops after 30s | Verify `Command refresh loop starting` appears in logs (fixed in latest) |

---

## Network Notes

- **Host networking** is used so the container can directly reach the inverter
  via Modbus TCP on the LAN (default `192.168.1.100:502`) and any MQTT broker
- If you use bridge networking instead, you'll need to ensure the container can
  route to the inverter IP — this typically doesn't work for Modbus TCP on a
  different subnet
- Dashboard is accessible on port 8080 from any device on the LAN

## Storage Notes

- Database file: `power_master.db` (SQLite, WAL mode)
- Writes are fsync'd (`synchronous=FULL`) to prevent corruption on hard shutdown
- WAL is checkpointed every 30 minutes and on clean shutdown
- Store the `/data` volume on a reliable filesystem — avoid USB drives on NAS
