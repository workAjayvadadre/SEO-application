# Deploying to the UAT VM (pm2 + nginx)

Needs on the VM: `git`, Python 3.10+ with `venv` (`sudo apt install python3-venv` on Ubuntu), `pm2`, `nginx`.

## First install

```bash
git clone https://github.com/workAjayvadadre/SEO-application.git keyword-studio
cd keyword-studio
bash deploy/deploy.sh          # creates .venv and .env, then stops so you can add keys
nano .env                      # SERPER_API_KEY, YOUTUBE_API_KEY, ANTHROPIC_API_KEY, BRAND_NAME ...
bash deploy/deploy.sh          # installs, starts under pm2, health-checks
```

Port 8000 taken by another service? `KEYWORD_STUDIO_PORT=8010 bash deploy/deploy.sh` (and use the same
port in nginx).

## nginx

```bash
sudo cp deploy/nginx-keyword-studio.conf /etc/nginx/sites-available/keyword-studio
sudo nano /etc/nginx/sites-available/keyword-studio        # set server_name (and port)
sudo ln -s /etc/nginx/sites-available/keyword-studio /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

(On RHEL/CentOS-style setups copy it to `/etc/nginx/conf.d/keyword-studio.conf` instead.)

To restrict the app to your domain only, start it with `ALLOWED_HOSTS=seo-uat.example.com bash deploy/deploy.sh`.

## Updating

```bash
cd keyword-studio && bash deploy/deploy.sh     # git pull + pip install + pm2 reload
```

Logs: `pm2 logs keyword-studio`. Status: `pm2 status`. Search history is kept in `./data`.

**There is no login.** Anyone who can reach the URL can run searches on your API keys.
