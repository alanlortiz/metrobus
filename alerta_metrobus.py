import os
import logging
import requests
import time
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_detallado(self) -> str:
        if not SCRAPER_API_KEY:
            return "❌ Error: Falta la clave de ScraperAPI."

        problemas = []
        # REACTIVAMOS JS (render: true) pero QUITAMOS la restricción de país (mx)
        # Esto usará los servidores globales premium para evitar el Error 500.
        parametros = {
            'api_key': SCRAPER_API_KEY,
            'url': self.url,
            'render': 'true'
        }
        
        intentos = 3
        html_content = ""
        
        # Sistema de reintentos para hacer el script indestructible
        for i in range(intentos):
            try:
                logging.info(f"Intento {i+1} de {intentos}: Esperando a que el JavaScript de la página dibuje la tabla...")
                respuesta = requests.get('http://api.scraperapi.com/', params=parametros, timeout=60)
                respuesta.raise_for_status()
                html_content = respuesta.text
                break  # Si tiene éxito y no colapsa, rompemos el ciclo
            except Exception as e:
                logging.warning(f"El proxy falló en el intento {i+1}: {e}")
                if i == intentos - 1:
                    return f"❌ Error extrayendo la página tras {intentos} intentos: {str(e)}"
                time.sleep(5) # Pausamos 5 segundos antes de volver a intentarlo

        logging.info("Analizando los datos reales...")
        soup = BeautifulSoup(html_content, 'html.parser')
        tablas = soup.find_all('table')

        if not tablas:
            logging.warning("No se detectaron tablas en el HTML.")

        for tabla in tablas:
            if 'estaciones afectadas' in tabla.text.lower():
                for fila in tabla.find_all('tr')[1:]:
                    celdas = fila.find_all('td')
                    if len(celdas) >= 3:
                        linea = celdas[0].get_text(strip=True)
                        est = celdas[1].get_text(strip=True)
                        afec = celdas[2].get_text(strip=True)
                        
                        est_limpio = est.lower().replace("estado", "").strip()
                        afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                        
                        if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                            # Formateamos bonito para que no mande un "1" suelto, sino "Línea 1"
                            problemas.append(f"🚇 *Línea {linea}:* {est} | {afec}")
        
        return "✅ Servicio Regular. Todo en orden para tu viaje." if not problemas else "⚠️ *AFECTACIONES DETECTADAS:*\n\n" + "\n".join(problemas)

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("ERROR CRÍTICO: Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"📍 *REPORTE METROBÚS*\n\n{mensaje}",
            "parse_mode": "Markdown"
        }
        
        try:
            requests.post(url, json=payload, timeout=15)
            logging.info("Mensaje de Telegram enviado con éxito.")
        except Exception as e:
            logging.error(f"Error enviando Telegram: {e}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
