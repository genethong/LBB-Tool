# Deployment Guide — Energy Analysis Tool

## Step 0 — Save the Edotco logo (do this first)
Save your `edotco_logo.png` file into:
```
neteco_tool/static/edotco_logo.png
```
The sidebar will then show the real PNG logo automatically.

---

## Part 1 — Push to GitHub

### 1.1 Install Git (if not already installed)
Open Terminal on your Mac and run:
```bash
git --version
```
If it says "command not found", download Git from https://git-scm.com/download/mac

### 1.2 Create a GitHub account
Go to https://github.com and sign up (free). Skip if you already have one.

### 1.3 Create a new repository on GitHub
1. Click the **+** button (top-right on GitHub) → **New repository**
2. Name it: `energy-analysis-tool`
3. Set it to **Private** (since it contains your NetEco server details)
4. Do NOT tick "Add README" or "Add .gitignore"
5. Click **Create repository**
6. Copy the repo URL — it looks like: `https://github.com/YOUR_USERNAME/energy-analysis-tool.git`

### 1.4 Push from your Mac
Open Terminal, then run these commands one by one:

```bash
# Go into the project folder
cd "/Users/jinthongteoh/Documents/Claude/Projects/LBB Tools/neteco_tool"

# Initialise git
git init

# Add all files (respects .gitignore — secrets in instance/ are excluded)
git add .

# First commit
git commit -m "Initial commit — Energy Analysis Tool"

# Connect to your GitHub repo (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/energy-analysis-tool.git

# Push to GitHub
git branch -M main
git push -u origin main
```

GitHub will ask for your username and password.
> **Note:** GitHub no longer accepts your account password here. You need a **Personal Access Token** instead:
> 1. Go to https://github.com/settings/tokens
> 2. Click **Generate new token (classic)**
> 3. Tick **repo** scope
> 4. Click Generate — copy the token
> 5. Use this token as the "password" when prompted in Terminal

---

## Part 2 — Deploy on GCP VM

### 2.1 SSH into your GCP VM
1. Go to https://console.cloud.google.com
2. Navigate to **Compute Engine → VM Instances**
3. Click **SSH** button next to your VM — a browser terminal opens

### 2.2 Install Python and Git on the VM
In the SSH terminal:
```bash
sudo apt update
sudo apt install -y python3 python3-pip git
```

### 2.3 Clone your repo onto the VM
```bash
# Go to home directory
cd ~

# Clone (replace YOUR_USERNAME)
git clone https://github.com/YOUR_USERNAME/energy-analysis-tool.git

# Enter the folder
cd energy-analysis-tool
```

### 2.4 Install Python dependencies
```bash
pip3 install -r requirements.txt --break-system-packages
```

### 2.5 Create the instance folder and config
```bash
mkdir -p instance
```

Now create your config file with your NetEco server details:
```bash
nano instance/config.json
```

Paste in your connection details (same as your local `instance/config.json`), then press `Ctrl+O` to save and `Ctrl+X` to exit.

### 2.6 Upload the logo to the VM
On your **local Mac**, open a new Terminal tab and run:
```bash
# Replace YOUR_VM_IP with your VM's External IP from GCP console
scp "/Users/jinthongteoh/Documents/Claude/Projects/LBB Tools/neteco_tool/static/edotco_logo.png" \
    YOUR_USERNAME@YOUR_VM_IP:~/energy-analysis-tool/static/edotco_logo.png
```

### 2.7 Open the firewall port on GCP
1. In GCP Console → **VPC Network → Firewall**
2. Click **Create Firewall Rule**
3. Fill in:
   - Name: `allow-8080`
   - Targets: **All instances in the network**
   - Source IP ranges: `0.0.0.0/0`
   - Protocols and ports: tick **TCP**, enter `8080`
4. Click **Create**

### 2.8 Start the app
Back in your SSH terminal:
```bash
cd ~/energy-analysis-tool
bash start_prod.sh
```

You should see:
```
================================================
  NetEco Tool (Production Mode)
  Running on port 8080
================================================
```

### 2.9 Access the tool
Open your browser and go to:
```
http://YOUR_VM_EXTERNAL_IP:8080
```

Your VM's External IP is shown in GCP Console → Compute Engine → VM Instances.

---

## Part 3 — Keep it running after you close SSH (optional)

By default the app stops when you close the SSH window. To keep it running permanently:

```bash
# Install screen
sudo apt install -y screen

# Start a persistent session
screen -S neteco

# Run the app
bash start_prod.sh

# Detach from screen (app keeps running)
# Press: Ctrl+A, then D

# To re-attach later:
screen -r neteco
```

---

## Part 4 — Update the app after code changes

Whenever you make changes on your Mac:
```bash
# On your Mac — commit and push
cd "/Users/jinthongteoh/Documents/Claude/Projects/LBB Tools/neteco_tool"
git add .
git commit -m "describe your change here"
git push
```

Then on your GCP VM:
```bash
cd ~/energy-analysis-tool
git pull
# Restart the app (Ctrl+C to stop, then run again)
bash start_prod.sh
```
