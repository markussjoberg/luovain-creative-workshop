# LuovAin! Creative Skills Week Workshop Platform

Tutkimuskäyttöön tarkoitettu Flask-sovellus luovien ammattilaisten AI-osaamisen kartoittamiseen ja ryhmämuodostukseen.

## 🎯 Sovelluksen tarkoitus

- **Osallistujien profilointi**: 5-7 vuoron keskustelu GPT-5:n kanssa
- **Ryhmien muodostus**: AI-avusteinen tasapainoisten tiimien luonti
- **Tutkimusdata**: Kaikki keskustelut ja profiilit tallennetaan PostgreSQL-tietokantaan
- **Reaaliaikainen hallinta**: Fasilitaattori-dashboard osallistujien seurantaan

## 🏗️ Arkkitehtuuri

```
┌─────────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Participants      │    │   Flask API      │    │  PostgreSQL     │
│   (creative.       │◄──►│   + GPT-5        │◄──►│   Database      │
│    agicola.fi)      │    │                  │    │                 │
└─────────────────────┘    └──────────────────┘    └─────────────────┘
                                    ▲
                           ┌──────────────────┐
                           │  Facilitator     │
                           │  Dashboard       │
                           │  (/facilitator)  │
                           └──────────────────┘
```

## 📋 Vaatimukset

### Pakolliset riippuvuudet:
- **Python 3.11+** 
- **PostgreSQL 15+**
- **OpenAI API -avain** (GPT-5 käyttöoikeus)
- **Cloudflare-tili** (julkinen pääsy)

### Python-paketit:
```bash
Flask==3.0.3
SQLAlchemy==2.0.32
psycopg2-binary==2.9.10
openai==1.51.2  
python-dotenv==1.0.1
numpy==2.1.1
```

## 🚀 Paikallinen asennus (Development)

### 1. Repositorion lataus
```bash
git clone <repository-url>
cd CreativeWeek
```

### 2. Virtuaaliympäristön luonti
```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# tai .venv\Scripts\activate  # Windows
```

### 3. Riippuvuuksien asennus
```bash
pip install -r requirements.txt
```

### 4. PostgreSQL:n asennus ja käynnistys

#### macOS (Homebrew):
```bash
brew install postgresql@15
brew services start postgresql@15

# Luo tietokanta
/opt/homebrew/opt/postgresql@15/bin/createdb creative_workshop
```

#### Ubuntu/Debian:
```bash
sudo apt update
sudo apt install postgresql-15 postgresql-client-15
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Luo tietokanta ja käyttäjä
sudo -u postgres psql
CREATE DATABASE creative_workshop;
CREATE USER workshop_user WITH PASSWORD 'secure_password_here';
GRANT ALL PRIVILEGES ON DATABASE creative_workshop TO workshop_user;
\q
```

#### CentOS/RHEL:
```bash
sudo dnf install postgresql15-server postgresql15
sudo postgresql-15-setup initdb
sudo systemctl start postgresql-15
sudo systemctl enable postgresql-15
```

### 5. Ympäristömuuttujien määrittely
Luo `.env` -tiedosto:
```bash
# OpenAI API
OPENAI_API_KEY=sk-proj-your-gpt5-api-key-here

# PostgreSQL (mukauta tarpeen mukaan)
CSW_DB_URL=postgresql://käyttäjä:salasana@localhost/creative_workshop

# Malli (valinnainen)
CSW_MODEL=gpt-5
```

### 6. Sovelluksen käynnistys
```bash
python3 CreativeTool.py
```

Sovellus käynnistyy osoitteessa: http://localhost:8000

## 🌐 Tuotantoasennus (Server Deployment)

### 1. Palvelinkonfiguraatio

#### Suositellut speksit 90 käyttäjälle:
- **CPU**: 4-8 ydintä
- **RAM**: 8-16 GB
- **Tallennustila**: 100+ GB (PostgreSQL + lokit)
- **Verkko**: Vakaa yhteys OpenAI API:in

### 2. PostgreSQL:n tuotantokonfiguraatio

#### `/etc/postgresql/15/main/postgresql.conf`:
```ini
# Yhteydet
max_connections = 200
shared_buffers = 2GB
effective_cache_size = 6GB

# Logging tutkimuskäyttöä varten
log_statement = 'all'
log_directory = '/var/log/postgresql'
log_filename = 'creative_workshop_%Y%m%d.log'
log_rotation_size = 100MB
```

#### `/etc/postgresql/15/main/pg_hba.conf`:
```
# Salli Flask-sovelluksen yhteydet
host    creative_workshop    workshop_user    127.0.0.1/32    md5
host    creative_workshop    workshop_user    ::1/128         md5
```

### 3. Gunicorn WSGI-palvelimen käyttö
```bash
# Asenna Gunicorn
pip install gunicorn

# Käynnistä tuotantotilassa
gunicorn -w 4 -b 0.0.0.0:8000 --timeout 120 CreativeTool:app
```

### 4. Nginx reverse proxy (suositeltava)

#### `/etc/nginx/sites-available/creative-workshop`:
```nginx
server {
    listen 80;
    server_name creative.agicola.fi;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket support (if needed later)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Timeouts for AI responses
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
        proxy_send_timeout 300;
    }
    
    # Static files
    location /static {
        alias /path/to/CreativeWeek/static;
        expires 30d;
    }
}
```

### 5. Systemd-palvelu

#### `/etc/systemd/system/creative-workshop.service`:
```ini
[Unit]
Description=LuovAin Creative Workshop Flask App
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=workshop
Group=workshop
WorkingDirectory=/home/workshop/CreativeWeek
Environment=PATH=/home/workshop/CreativeWeek/.venv/bin
EnvironmentFile=/home/workshop/CreativeWeek/.env
ExecStart=/home/workshop/CreativeWeek/.venv/bin/gunicorn -w 4 -b 127.0.0.1:8000 --timeout 120 CreativeTool:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Aktivoi palvelu
sudo systemctl daemon-reload
sudo systemctl enable creative-workshop
sudo systemctl start creative-workshop
```

## 📊 Tutkimusdata ja seuranta

### Tietokannan rakenne:

#### `participants` -taulu:
- Osallistujien perustiedot (UUID, nimi, luova rooli)
- Luontiaika tutkimustarkoituksiin

#### `chats` -taulu:
- Kaikki keskusteluviestit (käyttäjä ↔ AI)
- Aikaleima ja vaihe (onboarding/group)
- Täysi keskusteluhistoria tutkimusta varten

#### `participant_profiles` -taulu:
- AI:n generoima yhteenveto osallistujan tarpeista
- Osaamistaso ja tavoitteet
- Esteet AI-käytölle

#### `groups` ja `group_members` -taulut:
- Muodostetut ryhmät ja niiden jäsenet
- Ryhmämuodostuksen perustelu

### Datan varmuuskopiointi:
```bash
# Päivittäinen varmuuskopiointi
pg_dump creative_workshop > backup_$(date +%Y%m%d).sql

# Automatisointi crontabilla
0 3 * * * pg_dump creative_workshop > /backups/creative_workshop_$(date +\%Y\%m\%d).sql
```

## 🔧 Vianetsintä

### Yleiset ongelmat:

#### PostgreSQL-yhteysongelmat:
```bash
# Tarkista palvelun tila
sudo systemctl status postgresql

# Testaa yhteys
psql -h localhost -U workshop_user -d creative_workshop

# Lokien tarkistus
sudo tail -f /var/log/postgresql/postgresql-15-main.log
```

#### OpenAI API -ongelmat:
```bash
# Testaa API-avain
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
     https://api.openai.com/v1/models
```

#### Suorituskykyongelmat:
- **Tietokantayhteydet**: Nosta `pool_size` ja `max_overflow` arvoja
- **AI-vastausajat**: Nosta Nginx ja Gunicorn timeout-arvoja  
- **Muistinkäyttö**: Seuraa PostgreSQL `shared_buffers` käyttöä

### Lokien seuranta:
```bash
# Flask-sovellus
tail -f flask.log

# PostgreSQL
sudo tail -f /var/log/postgresql/creative_workshop_*.log

# Nginx
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

## 📈 Tuotannon valvonta

### Suositellut valvontamittarit:
- **Tietokantayhteydet**: PostgreSQL connection count
- **API-kutsut**: OpenAI API usage ja kustannukset
- **Vasteajat**: Flask response times
- **Virheet**: 5xx HTTP status codes
- **Muistinkäyttö**: Server resource usage

### Prometheus + Grafana -integraatio:
```python
# Lisää CreativeTool.py:iin
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
metrics = PrometheusMetrics(app)
```

## 🚨 Turvallisuus

### Suojattava data:
- **OpenAI API-avain**: Vain ympäristömuuttujassa
- **Tietokantasalasanat**: Encrypted at rest
- **Osallistujatunnisteet**: UUID-pohjaiset, ei henkilökohtaisia tietoja

### Firewall-säännöt:
```bash
# Salli vain tarpeelliset portit
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP (Nginx)
sudo ufw allow 443/tcp   # HTTPS (Nginx)
sudo ufw deny 8000/tcp   # Blokkaa suora Flask-yhteys
sudo ufw deny 5432/tcp   # Blokkaa suora PostgreSQL-yhteys
```

## 📞 Tuki ja ylläpito

### Kehittäjätuki:
- **Koodi**: [Repository URL]
- **Ongelmat**: GitHub Issues
- **Dokumentaatio**: README.md

### Tutkimusdata-access:
- PostgreSQL read-only -tunnukset tutkijoille
- Anonymisoitujen dataexporttien generointi
- GDPR-yhteensopivat tietojen poistotoiminnot

## 📝 Lisenssi

[Lisenssi-informaatio tähän]

---

**Viimeksi päivitetty**: 19.8.2025  
**Versio**: 1.0.0  
**Testattu**: Python 3.13, PostgreSQL 15, macOS/Ubuntu