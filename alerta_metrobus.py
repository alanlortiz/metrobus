import os
import logging
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

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
        try:
            logging.info("Lanzando Playwright a través del túnel proxy de ScraperAPI...")
            with sync_playwright() as p:
                # Usamos ScraperAPI solo como un túnel para que el Metrobús no bloquee a GitHub.
                # Playwright es el que dibujará la página web.
                browser = p.chromium.launch(
                    headless=True,
                    proxy={
                        "server": "http://proxy-server.scraperapi.com:8001",
                        "username": "scraperapi",
                        "password": SCRAPER_API_KEY
                    }
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                logging.info("Navegando al portal del Metrobús...")
                page.goto(self.url, wait_until="domcontentloaded", timeout=60000)

                # Le damos 5 segundos exactos al JavaScript del Metrobús para que dibuje la tabla roja
                logging.info("Esperando a que la página cargue los datos reales...")
                page.wait_for_timeout(5000) 

                html = page.content()
                browser.close()

            logging.info("Analizando los datos extraídos...")
            soup = BeautifulSoup(html, 'html.parser')
            tablas = soup.find_all('table')

            if not tablas:
                logging.warning("No se detectaron tablas.")

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
                                problemas.append(f"🚇 Línea {linea}: {est} | Cerradas: {afec}")
            
            return "✅ Servicio Regular. Todo en orden." if not problemas else "⚠️ AFECTACIONES DETECTADAS:\n\n" + "\n".join(problemas)

        except Exception as e:
            return f"❌ Error en la extracción: {str(e)}"

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"REPORTE METROBÚS\n\n{mensaje}"
            # Se eliminó el parse_mode="Markdown" para evitar bloqueos por caracteres especiales
        }
        
        try:
            respuesta = requests.post(url, json=payload, timeout=15)
            respuesta.raise_for_status() # Ahora SÍ te avisará en la consola si Telegram rechaza el mensaje
            logging.info("Mensaje de Telegram enviado con éxito.")
        except Exception as e:
            # Capturamos el error real de Telegram para poder leerlo
            detalle = respuesta.text if 'respuesta' in locals() else "Sin respuesta"
            logging.error(f"Error enviando Telegram: {e}. Detalle: {detalle}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
