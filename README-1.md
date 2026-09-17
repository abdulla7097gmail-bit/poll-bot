# কাস্টম পোল টেলিগ্রাম বট

বাটনের মাধ্যমে নিজের ইচ্ছামত নাম দিয়ে আনলিমিটেড পোল তৈরি করা যায়, যেকোনো চ্যানেল/গ্রুপে
পোস্ট করা যায় (যেখানে বট এডমিন), এবং শুধু সেই চ্যানেল/গ্রুপের এডমিনরাই পোল বন্ধ করতে পারবে।

## ফিচার

- `/newpoll` বা মেনু বাটন থেকে ধাপে ধাপে (প্রশ্ন → অপশন → পাবলিশ) পোল তৈরি
- ইচ্ছামত সংখ্যক অপশন যোগ (২ থেকে ২০টা)
- বট যেই চ্যানেল/গ্রুপে এডমিন, সবগুলোতে পোল বানানো যাবে — সীমাহীন
- একজন ইউজার একবারই ভোট দিতে পারবে, আবার ভোট দিলে আগের ভোট বদলে যাবে
- শুধু ওই চ্যাটের **এডমিন** (বা পোল যিনি বানিয়েছেন) পোল বন্ধ করতে পারবেন
- পোল বন্ধ হলে ফলাফল র‍্যাংক করে (🥇🥈🥉) দেখানো হয়
- SQLite ডাটাবেজ — কোনো আলাদা সার্ভার লাগে না

## ফাইল স্ট্রাকচার

```
pollbot/
├── bot.py            # মূল বট লজিক
├── database.py       # SQLite ডাটাবেজ লেয়ার
├── requirements.txt  # প্রয়োজনীয় প্যাকেজ
├── .env.example      # টোকেন রাখার নমুনা ফাইল
└── .gitignore
```

## ১) বট টোকেন সংগ্রহ

1. টেলিগ্রামে **@BotFather**-কে মেসেজ দিন
2. `/newbot` কমান্ড দিন, নাম ও ইউজারনেম দিন
3. যে টোকেন পাবেন (`123456789:ABC...`), সেটা সংরক্ষণ করুন

## ২) GitHub-এ কোড আপলোড

মোবাইল থেকে (ডেস্কটপ ছাড়া) সহজ উপায়:

**অপশন A — GitHub মোবাইল অ্যাপ / ওয়েবসাইট দিয়ে:**
1. GitHub-এ নতুন **repository** বানান (যেমন `poll-bot`)
2. "Add file" → "Upload files" চেপে এই ৫টা ফাইল (`bot.py`, `database.py`,
   `requirements.txt`, `.env.example`, `.gitignore`) আপলোড করুন
3. Commit করুন

**অপশন B — Termux (Android) দিয়ে git কমান্ড:**
```bash
pkg install git
git clone https://github.com/<আপনার-ইউজারনেম>/poll-bot.git
cd poll-bot
# ফাইলগুলো কপি করুন এই ফোল্ডারে
git add .
git commit -m "poll bot প্রথম ভার্সন"
git push origin main
```

⚠️ `.env` ফাইল (আসল টোকেনসহ) কখনো GitHub-এ আপলোড করবেন না — `.gitignore` এটা এমনিতেই বাদ দেবে।

## ৩) কোথায় রান করবেন

এই বট `long polling` পদ্ধতিতে চলে (Telegram সার্ভার থেকে বারবার মেসেজ চেক করে), তাই এটাকে
**সবসময় চালু** এমন একটা জায়গায় রাখতে হবে। মোবাইল-বেজড ডেভেলপার হিসেবে সবচেয়ে সহজ ৩টা উপায়:

### Railway.app (সবচেয়ে সহজ, GitHub থেকে সরাসরি ডিপ্লয়)
1. https://railway.app এ GitHub দিয়ে লগইন করুন
2. "New Project" → "Deploy from GitHub repo" → আপনার `poll-bot` repo সিলেক্ট করুন
3. Settings → Variables এ গিয়ে যোগ করুন: `BOT_TOKEN = আপনার-টোকেন`
4. Start command না থাকলে দিন: `python bot.py`
5. Deploy শেষ হলেই বট চালু হয়ে যাবে (Logs ট্যাবে "বট চালু হচ্ছে..." দেখাবে)
6. ⚠️ Railway-তে ডিফল্ট ফাইলসিস্টেম প্রতি redeploy-তে রিসেট হতে পারে — এতে `pollbot.db`
   হারিয়ে যাবে (পুরনো পোল/ভোট মুছে যাবে, নতুন পোল ঠিকই কাজ করবে)। স্থায়ী রাখতে চাইলে
   Railway-এর **Volume** যোগ করে `DB_PATH=/data/pollbot.db` সেট করুন।

### একটা VPS (সবচেয়ে স্থায়ী সমাধান)
```bash
sudo apt update && sudo apt install python3-pip python3-venv git -y
git clone https://github.com/<আপনার-ইউজারনেম>/poll-bot.git
cd poll-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # BOT_TOKEN বসান

# বট ২৪/৭ চালু রাখতে systemd সার্ভিস বানান:
sudo nano /etc/systemd/system/pollbot.service
```
`pollbot.service`-এ লিখুন:
```ini
[Unit]
Description=Telegram Poll Bot
After=network.target

[Service]
WorkingDirectory=/root/poll-bot
ExecStart=/root/poll-bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/root/poll-bot/.env

[Install]
WantedBy=multi-user.target
```
তারপর:
```bash
sudo systemctl daemon-reload
sudo systemctl enable pollbot
sudo systemctl start pollbot
sudo systemctl status pollbot   # চলছে কিনা যাচাই
```

### Render.com — সম্পূর্ণ ফ্রি (সঙ্গে সীমাবদ্ধতা আছে)

⚠️ **আগে জেনে নিন:** Render-এর ফ্রি tier শুধু "Web Service" টাইপকে ফ্রি রাখে, আর
১৫ মিনিট কোনো HTTP request না পেলে পুরো প্রসেসটাই ঘুমিয়ে (sleep) যায় — মানে বটও
থেমে যায়। এটা এড়াতে নিচে একটা ফ্রি "keep-alive" ট্রিক দেওয়া হলো। এই কোডে আগে থেকেই
একটা ছোট health-check সার্ভার (`start_health_server()`) যোগ করা আছে যাতে Render
প্রসেসটাকে "Web Service" হিসেবে চালাতে দেয়।

**ধাপ ১ — Render-এ ডিপ্লয়:**
1. https://render.com এ গিয়ে GitHub দিয়ে সাইন আপ করুন
2. Dashboard → "New +" → "Web Service"
3. আপনার `poll-bot` GitHub repo সিলেক্ট করুন
4. সেটিংস দিন:
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** Free
5. "Environment" ট্যাবে যোগ করুন: `BOT_TOKEN = আপনার-টোকেন`
6. "Create Web Service" চাপুন — বিল্ড শেষ হলে Logs-এ "বট চালু হচ্ছে..." এবং
   "health-check সার্ভার চালু হয়েছে..." দেখাবে
7. উপরে একটা URL পাবেন যেমন `https://poll-bot-xxxx.onrender.com` — এটা কপি রাখুন

**ধাপ ২ — ঘুমিয়ে যাওয়া ঠেকান (ফ্রি "UptimeRobot" দিয়ে):**
1. https://uptimerobot.com এ ফ্রি অ্যাকাউন্ট খুলুন
2. "Add New Monitor" → Monitor Type: **HTTP(s)**
3. URL-এ ধাপ ১-এর Render URL বসান
4. Monitoring Interval: **৫ মিনিট**
5. Save করুন

এখন UptimeRobot প্রতি ৫ মিনিটে আপনার Render সার্ভিসে একটা রিকোয়েস্ট পাঠাবে, ফলে
Render এটাকে "active" মনে করবে এবং বট চালু থাকবে।

**সীমাবদ্ধতা যা মেনে নিতে হবে:**
- মাঝেমধ্যে Render নিজে থেকে রিস্টার্ট করলে `pollbot.db` মুছে যেতে পারে (ফ্রি tier-এ
  persistent disk নেই) — পুরনো পোল/ভোট হারাবে, কিন্তু নতুন পোল ঠিকই কাজ করবে
- ফ্রি tier মাসে নির্দিষ্ট ঘণ্টা (৭৫০ ঘন্টা) পর্যন্ত চলে — একটা সার্ভিস একাই চালালে
  সাধারণত পুরো মাস কভার হয়ে যায়
- এটা ২৪/৭ স্থায়ী প্রোডাকশনের জন্য আদর্শ না — বড় হলে Oracle Cloud Always Free VM
  বা একটা সস্তা VPS-এ (উপরের systemd পদ্ধতি) সরানো ভালো

## ৪) লোকালি (নিজের ফোনে Termux দিয়ে) টেস্ট

```bash
pkg install python git
cd poll-bot
pip install -r requirements.txt
cp .env.example .env
# .env এ BOT_TOKEN বসান
python bot.py
```
টার্মিনালে "বট চালু হচ্ছে..." দেখলে বট লাইভ — এবার টেলিগ্রামে গিয়ে `/start` দিন।

## ৫) বট ব্যবহারের নিয়ম

1. বটকে যেই চ্যানেল/গ্রুপে পোল বানাতে চান, সেখানে **Add Admin** করুন
   (কমপক্ষে "Post Messages" / "Send Messages" পারমিশন দিন)
2. টেলিগ্রামে বটকে `/start` দিন
3. "📊 নতুন পোল তৈরি করুন" চাপুন
4. তালিকা থেকে চ্যানেল/গ্রুপ বেছে নিন (শুধু সেগুলোই দেখাবে যেখানে আপনি নিজে এডমিন এবং বটও এডমিন)
5. পোলের প্রশ্ন লিখুন
6. একটার পর একটা অপশনের নাম লিখুন, শেষ হলে "✅ শেষ করুন" চাপুন
7. পোল পোস্ট হয়ে যাবে — সবাই বাটনে ভোট দিতে পারবে
8. চ্যানেল/গ্রুপের এডমিন "🔴 থামান এবং ফলাফল পান" চেপে পোল বন্ধ ও ফলাফল দেখতে পারবেন

## নোট

- এই কোড এই sandbox-এ syntax, handler-registration, এবং vote/close লজিক বাস্তবে চালিয়ে
  যাচাই করা হয়েছে — কিন্তু আসল Telegram সার্ভারের বিপরীতে লাইভ রান করে দেখা যায়নি
  (এই পরিবেশ থেকে `api.telegram.org`-এ নেটওয়ার্ক এক্সেস নেই)। তাই প্রথমবার নিজের ফোনে
  বা হোস্টে চালিয়ে `/start` টেস্ট করে নেওয়া জরুরি।
- বড় স্কেলে (হাজার হাজার ভোট/মিনিট) গেলে SQLite-এর বদলে PostgreSQL/Supabase-এ সরানো ভালো —
  এখনকার ডিজাইন সেভাবেই আলাদা `database.py` মডিউলে রাখা হয়েছে, যাতে পরে সহজে বদলানো যায়।
