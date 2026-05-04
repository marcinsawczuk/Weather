from flask import Flask, render_template, request, redirect
import sqlite3
import paho.mqtt.client as mqtt
import datetime
import threading
import time
import requests

app = Flask(__name__)

# ===== KONFIG =====
MQTT_BROKER = "localhost"
API_KEY = "202f96a279432867be53e38c64863df1"
CITY = "Warsaw"

# ===== DB =====
def get_db():
    return sqlite3.connect("podlewanie.db", timeout=10, check_same_thread=False)

def init_db():
    conn = sqlite3.connect("podlewanie.db")
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    conn.commit()
    conn.close()

init_db()

# ===== MQTT =====
client = mqtt.Client()
client.connect(MQTT_BROKER, 1883, 60)
client.loop_start()

# ===== GLOBALNE =====
last_on = {}
is_watering = False
last_run = None
last_run_day = None

# ===== MQTT SEND =====
def send(sekcja, stan):
    try:
        client.publish(f"podlewanie/{sekcja}", stan)

        conn = get_db()
        c = conn.cursor()

        if stan == "ON":
            last_on[sekcja] = datetime.datetime.now()

        elif stan == "OFF" and sekcja in last_on:
            diff = datetime.datetime.now() - last_on[sekcja]
            minutes = diff.total_seconds() / 60

            c.execute("INSERT INTO czas_podlewania (sekcja, czas_min) VALUES (?,?)",
                      (sekcja, minutes))

        c.execute("INSERT INTO logi (sekcja, stan) VALUES (?,?)", (sekcja, stan))

        conn.commit()
        conn.close()

    except Exception as e:
        print("❌ send error:", e)

# ===== POGODA =====
from datetime import datetime, timedelta
import requests

def will_rain_tomorrow():
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={CITY}&appid={API_KEY}&units=metric"
        r = requests.get(url, timeout=5)

        if r.status_code != 200:
            print("❌ API error:", r.status_code)
            return True  # bezpiecznie: nie podlewaj

        data = r.json()

        tomorrow = (datetime.now() + timedelta(days=1)).date()

        total_rain = 0
        rain_hours = 0

        for item in data.get("list", []):
            dt = datetime.strptime(item["dt_txt"], "%Y-%m-%d %H:%M:%S")

            if dt.date() == tomorrow:

                # 👉 tylko godziny dzienne
                if 6 <= dt.hour <= 22:

                    pop = item.get("pop", 0)
                    rain = item.get("rain", {}).get("3h", 0)

                    # 👉 liczymy tylko sensowny deszcz
                    if pop > 0.3:
                        total_rain += rain
                        if rain > 0:
                            rain_hours += 1

        print(f"🌧️ prognoza jutro: {total_rain:.2f} mm, {rain_hours} bloków")

        # ===== LOGIKA DECYZJI =====

        # mocny deszcz → nie podlewaj
        if total_rain >= 2:
            return True

        # trochę deszczu ale rozłożony → też nie podlewaj
        if rain_hours >= 2:
            return True

        return False

    except Exception as e:
        print("❌ Rain check error:", e)
        return True  # bezpiecznie: NIE podlewaj przy błędzie

# ===== AI =====
def should_water():
    if will_rain_tomorrow():
        print("🌧️ Pomijam (deszcz)")
        return False

    try:
        conn = get_db()
        c = conn.cursor()

        last = c.execute("SELECT wartosc FROM wilgotnosc ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()

        if last:
            value = last[0]

            if value > 2500:
                return True
            elif value > 1800:
                return "SHORT"
            else:
                return False

    except:
        pass

    return True

# ===== PANEL =====
@app.route("/")
def index():
    conn = get_db()
    c = conn.cursor()

    wilgotnosc = list(c.execute("SELECT * FROM wilgotnosc ORDER BY id DESC LIMIT 20"))

    czas = list(c.execute("""
        SELECT sekcja, SUM(czas_min)
        FROM czas_podlewania
        WHERE date(data) = date('now','localtime')
        GROUP BY sekcja
    """))

    harmonogram = list(c.execute("""
        SELECT id, godzina, minuta, s1, s2, s3, s4, s5, s6
        FROM harmonogram
    """))

    conn.close()

    return render_template("index.html",
                           wilgotnosc=wilgotnosc,
                           czas=czas,
                           rainTomorrow=will_rain_tomorrow(),
                           harmonogram=harmonogram)

# ===== STEROWANIE =====
@app.route("/on/<int:s>")
def on(s):
    send(s, "ON")
    return redirect("/")

@app.route("/off/<int:s>")
def off(s):
    send(s, "OFF")
    return redirect("/")

# ===== WILGOTNOŚĆ =====
@app.route("/moisture", methods=["POST"])
def moisture():
    val = int(request.form["value"])

    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO wilgotnosc (wartosc) VALUES (?)", (val,))
    conn.commit()
    conn.close()

    return "OK"

# ===== DODAWANIE HARMONOGRAMU =====
@app.route("/add_schedule", methods=["POST"])
def add_schedule():
    godzina = int(request.form["godzina"])
    minuta = int(request.form["minuta"])

    s1 = int(request.form["s1"]) * 60
    s2 = int(request.form["s2"]) * 60
    s3 = int(request.form["s3"]) * 60
    s4 = int(request.form["s4"]) * 60
    s5 = int(request.form["s5"]) * 60
    s6 = int(request.form["s6"]) * 60

    conn = get_db()
    c = conn.cursor()

    c.execute("""
        INSERT INTO harmonogram (godzina, minuta, s1, s2, s3, s4, s5, s6)
        VALUES (?,?,?,?,?,?,?,?)
    """, (godzina, minuta, s1, s2, s3, s4, s5, s6))

    conn.commit()
    conn.close()

    return redirect("/")

# ===== USUWANIE =====
@app.route("/delete/<int:id>")
def delete(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM harmonogram WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect("/")

# ===== SCHEDULER =====
def scheduler():
    global last_run, last_run_day, is_watering

    print("🚀 Scheduler start")

    while True:
        try:
            now = datetime.now()
            today = now.date()

            conn = get_db()
            c = conn.cursor()

            for row in c.execute("""
                SELECT godzina, minuta, s1, s2, s3, s4, s5, s6
                FROM harmonogram
            """):

                godzina, minuta, *sekcje = row

                if now.hour == godzina and now.minute == minuta:

                    if last_run == (godzina, minuta) and last_run_day == today:
                        continue

                    if is_watering:
                        continue

                    last_run = (godzina, minuta)
                    last_run_day = today

                    decision = should_water()

                    if decision is False:
                        print("🚫 Pominięto")
                        continue

                    is_watering = True
                    print("💧 START")

                    for i, duration in enumerate(sekcje, start=1):

                        if duration <= 0:
                            continue

                        if decision == "SHORT":
                            duration = int(duration / 2)

                        duration = max(1, int(duration))

                        print(f"➡️ Sekcja {i} {duration}s")

                        send(i, "ON")
                        time.sleep(duration)
                        send(i, "OFF")

                        time.sleep(2)

                    is_watering = False
                    print("✅ KONIEC")

            conn.close()

        except Exception as e:
            print("🔥 Scheduler error:", e)

        time.sleep(10)

# ===== START =====
if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, use_reloader=False)
