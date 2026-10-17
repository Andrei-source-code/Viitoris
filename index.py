import os
import json
import unicodedata

import requests
import pandas as pd
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------- configurare
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Poți fixa modelul din Vercel (variabila GEMINI_MODEL); altfel se încearcă pe rând
MODELE_GEMINI = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.5-flash",
                             "gemini-2.5-flash", "gemini-flash-latest"] if m]

# Groq rămâne ca rezervă, doar dacă ai setat GROQ_API_KEY
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
try:
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except Exception:
    client = None

# Toate fisierele sunt in acelasi folder cu index.py (radacina repo-ului)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = BASE_DIR
CSV_PATH = os.path.join(DATA_DIR, "baza_cariere.csv")
JSON_PATH = os.path.join(DATA_DIR, "surse_piata.json")

CATEGORII = ['REALIST', 'INVESTIGATIV', 'ARTISTIC',
             'SOCIAL', 'INTREPRINZATOR', 'CONVENTIONAL']


# ------------------------------------------------------------- încărcare date
def incarca_csv():
    for enc in ("utf-8-sig", "cp1250", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(CSV_PATH, encoding=enc, sep=None, engine="python")
            df.columns = [str(c).strip() for c in df.columns]
            # numele reale din CSV conțin explicații ("facultate_ro / Școală"),
            # le aducem la numele scurte folosite în cod
            redenumiri = {}
            for c in df.columns:
                baza = c.split("/")[0].split("(")[0].strip()
                if baza != c:
                    redenumiri[c] = baza
            df = df.rename(columns=redenumiri)
            # elimină rândurile goale
            df = df.dropna(subset=["id_cariera", "cod_psihologic"])
            if "cod_psihologic" in df.columns:
                df["cod_psihologic"] = (
                    df["cod_psihologic"].astype(str).str.strip().str.upper()
                )
            print(f"baza_cariere.csv incarcat (encoding: {enc}).")
            return df
        except FileNotFoundError:
            print(f"Fisierul {CSV_PATH} nu exista.")
            return None
        except Exception as e:
            print(f"Incercare {enc} esuata: {e}")
    return None


def incarca_surse():
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"surse_piata.json nu a putut fi incarcat: {e}")
        return {}


df_cariere = incarca_csv()
SURSE = incarca_surse()


# ------------------------------------------------------------------- utilitare
def fara_diacritice(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text)
                   if unicodedata.category(c) != 'Mn')


def citeste_date():
    """Acceptă JSON (din JavaScript) sau form-encoded (webhook Forminator)."""
    js = request.get_json(silent=True)
    if isinstance(js, dict):
        return js
    return {k: (v if len(v) > 1 else v[0]) for k, v in request.form.lists()}


# ------------------------------------------------------------- partea CARIERĂ
def calculeaza_punctaje(data):
    if any(f'punctaj_{c.lower()}' in data for c in CATEGORII):
        out = {}
        for c in CATEGORII:
            try:
                out[c] = int(float(data.get(f'punctaj_{c.lower()}', 0)))
            except (TypeError, ValueError):
                out[c] = 0
        return out

    out = {c: 0 for c in CATEGORII}
    for valoare in data.values():
        for v in (valoare if isinstance(valoare, list) else [valoare]):
            cod = fara_diacritice(str(v)).strip().upper()
            if cod in out:
                out[cod] += 1
    return out


# --------------------------------------------------------- partea ANTREPRENOR
# cheie -> (cuvinte cheie, filtre OpenStreetMap)
# ORDINEA CONTEAZĂ: "salon" înaintea lui "bar" (ca "barber" să nu fie bar)
TIPURI = {
    "salon": (["salon", "barber", "frizer", "coafor", "infrumuset", "beauty",
               "manichiur", "cosmetic"],
              ['["shop"~"^(hairdresser|beauty)$"]']),
    "patiserie": (["patiser", "brutar", "covrig", "panific", "plachin"],
                  ['["shop"="bakery"]']),
    "cafenea": (["cafenea", "cafe", "coffee", "ceainar"],
                ['["amenity"="cafe"]']),
    "restaurant": (["restaurant", "bistro", "pizz", "fast", "mancare", "grill"],
                   ['["amenity"~"^(restaurant|fast_food)$"]']),
    "fitness": (["fitness", "gym", "sala de sport", "sport"],
                ['["leisure"="fitness_centre"]']),
    "bar": (["bar", "pub", "club"],
            ['["amenity"~"^(bar|pub|nightclub)$"]']),
    # fără buget minim în JSON, dar cu căutare pe hartă
    "stomatologie": (["stomatolog", "dentar", "dentist"], ['["amenity"="dentist"]']),
    "farmacie": (["farmacie"], ['["amenity"="pharmacy"]']),
    "service_auto": (["service auto", "auto", "vulcaniz", "service"],
                     ['["shop"="car_repair"]']),
}


def detecteaza_tip(tip_afacere):
    t = fara_diacritice(tip_afacere).lower()
    for cheie, (cuvinte, _) in TIPURI.items():
        if any(c in t for c in cuvinte):
            return cheie
    return None


def buget_minim_pentru(cheie):
    """Returnează (buget_minim, nume_afacere, sursa) sau (None, None, None)."""
    info = SURSE.get(cheie) if cheie else None
    if not info:
        return None, None, None
    loc = next(iter(info.get("locatii", {}).values()), {})
    return loc.get("buget_minim_necesar"), info.get("nume"), loc.get("sursa_informatii")


def numara_competitori_live(oras, cheie):
    """Număr de afaceri similare din oraș (OpenStreetMap). None dacă nu se poate."""
    if not cheie:
        return None
    filtre = TIPURI[cheie][1]
    oras_safe = oras.replace('"', '').replace('\\', '')
    interogari = "\n".join(f"nwr{f}(area.a);" for f in filtre)
    query = f"""
    [out:json][timeout:15];
    area["name"="{oras_safe}"]["boundary"="administrative"]->.a;
    (
      {interogari}
    );
    out count;
    """
    try:
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={'data': query},
            headers={'User-Agent': 'viitoris-business-analyzer/1.0'},
            timeout=18,
        )
        r.raise_for_status()
        elements = r.json().get('elements', [])
        if elements:
            return int(elements[0].get('tags', {}).get('total', 0))
        return 0
    except Exception as e:
        print(f"Avertisment Overpass: {e}")
        return None


def evalueaza(buget, buget_min, competitori):
    """Verdict calculat de cod (determinist). AI-ul doar îl explică."""
    scor = 0
    if buget_min:
        raport = buget / buget_min
        if raport >= 1.5:
            scor += 2
        elif raport >= 1:
            scor += 1
        elif raport >= 0.7:
            scor -= 1
        else:
            scor -= 3
    if competitori is not None:
        if competitori <= 10:
            scor += 2
        elif competitori <= 30:
            scor += 1
        elif competitori <= 60:
            scor -= 1
        else:
            scor -= 2
    return ("REUȘITĂ" if scor >= 1 else "EȘEC"), scor


def text_rezerva(rezultat, oras, tip, buget, buget_min, competitori):
    """Raport simplu, folosit dacă AI-ul nu răspunde."""
    p1 = (f"Pentru o afacere de tip {tip} în {oras} cu un buget de {buget:.0f} EUR, "
          f"evaluarea automată este: {rezultat}.")
    if competitori is not None:
        p1 += f" Pe hartă au fost găsiți {competitori} competitori similari."
    p2 = ""
    if buget_min:
        if buget >= buget_min:
            p2 = (f"Bugetul depășește minimul orientativ de {buget_min} EUR, "
                  f"deci acoperă investiția inițială.")
        else:
            p2 = (f"Bugetul este sub minimul orientativ de {buget_min} EUR, "
                  f"ceea ce crește riscul. Luați în calcul o locație mai mică "
                  f"sau echipamente second-hand.")
    return p1 + ("\n\n" + p2 if p2 else "")


def genereaza_raport(prompt):
    """Încearcă Gemini (pe rând, mai multe modele), apoi Groq. Returnează text sau ''."""
    if GEMINI_API_KEY:
        for model in MODELE_GEMINI:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_API_KEY,
                             "Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.4,
                                               "maxOutputTokens": 1200}},
                    timeout=30,
                )
                if r.status_code != 200:
                    print(f"Gemini {model}: HTTP {r.status_code} {r.text[:200]}")
                    continue
                parti = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parti).strip()
                if text:
                    return text
            except Exception as e:
                print(f"Gemini {model} eroare: {e}")
    if client is not None:
        try:
            c = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.4, max_tokens=700, timeout=30)
            return c.choices[0].message.content.strip()
        except Exception as e:
            print(f"Eroare Groq: {e}")
    return ""


# ---------------------------------------------------------------------- rute
@app.route('/')
def health():
    return jsonify({
        'status': 'ok',
        'cariere_incarcate': df_cariere is not None,
        'gemini_configurat': GEMINI_API_KEY is not None,
        'groq_configurat': client is not None,
    })


@app.route('/predict-cariera', methods=['POST'])
def predict_cariera():
    if df_cariere is None:
        return jsonify({'error': 'Baza de date cu cariere nu este disponibilă.'}), 500

    punctaje = calculeaza_punctaje(citeste_date())
    scor_maxim = max(punctaje.values())
    if scor_maxim == 0:
        return jsonify({'error': 'Nu s-au primit răspunsuri valide din test.'}), 400

    profil = max(punctaje, key=punctaje.get)
    potrivite = df_cariere[df_cariere['cod_psihologic'] == profil]
    if potrivite.empty:
        return jsonify({'error': f'Nu există cariere pentru profilul {profil}'}), 404

    cariera = potrivite.iloc[scor_maxim % len(potrivite)]
    try:
        return jsonify({
            'status': 'success',
            'id_cariera': int(cariera['id_cariera']),
            'profil_detectat': profil,
            'punctaje': punctaje,
            'descriere_profil': str(cariera.get('descriere_profil', '')),
            'punctaj_obtinut': scor_maxim,
            'titlu_job': str(cariera['titlu_job']),
            'facultate_ro': str(cariera['facultate_ro']),
            'admitere_ro': str(cariera['admitere_ro']),
            'facultate_strainatate': str(cariera['facultate_strainatate']),
            'admitere_strainatate': str(cariera['admitere_strainatate']),
            'pas_practic': str(cariera['pas_practic']),
            'salariu_mediu': int(cariera['salariu_mediu']),
        })
    except KeyError as e:
        return jsonify({'error': f'Coloana {e} lipsește din baza_cariere.csv'}), 500
    except (TypeError, ValueError) as e:
        return jsonify({'error': f'Valoare invalidă în CSV: {e}'}), 500


@app.route('/predict', methods=['POST'])
def analizeaza_business_live():
    data = citeste_date()
    try:
        buget = float(data.get('buget', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Bugetul trebuie să fie un număr.'}), 400
    if buget <= 0:
        return jsonify({'error': 'Bugetul trebuie să fie mai mare ca zero.'}), 400

    oras = str(data.get('oras', 'Bacău')).strip() or 'Bacău'
    tip_afacere = str(data.get('tip_afacere', 'Cafenea')).strip() or 'Cafenea'

    cheie = detecteaza_tip(tip_afacere)
    buget_min, nume_oficial, sursa = buget_minim_pentru(cheie)
    competitori = numara_competitori_live(oras, cheie)
    rezultat, scor = evalueaza(buget, buget_min, competitori)

    raport = ""
    if GEMINI_API_KEY or client is not None:
        linie_comp = (f"{competitori} competitori similari găsiți pe harta OpenStreetMap"
                      if competitori is not None
                      else "numărul de competitori nu a putut fi determinat")
        linie_min = (f"Bugetul minim orientativ pentru acest tip de afacere este "
                     f"{buget_min} EUR (sursa: {sursa})."
                     if buget_min else
                     "Nu există un buget minim de referință; estimează-l tu prudent.")
        prompt = f"""
Ești un expert în analiză de piață și fezabilitate financiară din România.

DATE:
- Oraș: {oras}
- Tip afacere: {tip_afacere}
- Buget disponibil: {buget:.0f} EUR
- Competitori: {linie_comp}
- {linie_min}

VERDICTUL a fost deja calculat de sistem: {rezultat}. Nu îl schimba.

SARCINĂ: scrie în română un raport de 2-3 paragrafe scurte, FĂRĂ titlu și fără
a repeta cuvântul „verdict":
1. Menționează numărul de competitori din {oras} (dacă îl cunoști) și ce
   înseamnă pentru concurență.
2. Explică dacă bugetul de {buget:.0f} EUR este suficient (chirie, echipamente,
   autorizări, promovare) și justifică verdictul {rezultat}.
3. Încheie cu o recomandare strategică concretă și realistă.
Nu inventa cifre exacte pe care nu le ai.
"""
        raport = genereaza_raport(prompt)
    ai_ok = bool(raport)

    if not ai_ok:
        raport = text_rezerva(rezultat, oras, tip_afacere, buget, buget_min, competitori)

    return jsonify({
        'rezultat': rezultat,
        'explicatie': raport,
        'competitori_gasiti': competitori,
        'oras': oras,
        'buget_minim': buget_min,
        'tip_detectat': nume_oficial or tip_afacere,
        'raport_ai': ai_ok,
    })


# Doar pentru rulare locală
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
