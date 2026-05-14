import os
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

MI_NUMERO = os.getenv("MI_NUMERO")
API_KEY = os.getenv("API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_detallado(self) -> str:
        problemas = []
        try:
            logging.info("Lanzando navegador headless con Playwright...")
            with sync_playwright() as p:
                # Lanzamos Chromium de forma invisible
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
                )
                page = context.new_page()
                
                # Vamos a la página y esperamos hasta que la red esté inactiva (garantiza que el JS ya cargó la tabla)
                logging.info("Navegando al portal del Metrobús y esperando renderizado...")
                page.goto(self.url, wait_until="networkidle", timeout=60000)
                
                # Extraemos el HTML ya procesado
                html = page.content()
                browser.close()

            logging.info("Analizando el DOM...")
            soup = BeautifulSoup(html, 'html.parser')
            tablas = soup.find_all('table')
            
            if not tablas:
                logging.warning("No se detectaron tablas en el HTML renderizado.")

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
                                problemas.append(f"• {linea}: {est} | {afec}")
            
            return "✅ *Servicio Regular*" if not problemas else "⚠️ *AFECTACIONES DETECTADAS:*\n" + "\n".join(problemas)

        except Exception as e:
            return f"❌ Error ejecutando Playwright: {str(e)}"

    def enviar_whatsapp(self, mensaje: str) -> None:
        if not MI_NUMERO or not API_KEY:
            logging.error("Faltan las credenciales de CallMeBot.")
            return
        
        msg_codificado = urllib.parse.quote(f"🚇 *REPORTE METROBÚS*\n\n{mensaje}")
        url = f"https://api.callmebot.com/whatsapp.php?phone={MI_NUMERO}&text={msg_codificado}&apikey={API_KEY}"
        
        try:
            respuesta = requests.get(url, timeout=15)
            respuesta.raise_for_status()
            logging.info("WhatsApp enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Fallo al enviar el WhatsApp: {str(e)}")

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_whatsapp(monitor.obtener_estado_detallado())
