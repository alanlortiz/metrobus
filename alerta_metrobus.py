import os
import logging
import re
import requests
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
            return "Error: Falta la clave de ScraperAPI en los Secrets de GitHub."

        problemas = []
        try:
            logging.info("Consultando la página del Metrobús usando ScraperAPI...")
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'country_code': 'mx',
                'render': 'true'
            }
            
            # Usamos timeout de 60 porque el renderizado JS toma tiempo
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')

            for tabla in tablas:
                if 'estaciones afectadas' in tabla.text.lower():
                    # Brincamos la fila 0 porque son los títulos
                    for fila in tabla.find_all('tr')[1:]:
                        try:
                            celdas = fila.find_all('td')
                            if len(celdas) >= 3:
                                # 1. INTENTAR EXTRAER LA LÍNEA (Texto o Logo)
                                linea = celdas[0].get_text(strip=True)
                                
                                if not linea:
                                    img = celdas[0].find('img')
                                    if img:
                                        linea = img.get('alt') or img.get('title') or ""
                                        if not linea:
                                            src = img.get('src', '')
                                            match = re.search(r'linea_?(\d+)', src.lower())
                                            if match:
                                                linea = f"Línea {match.group(1)}"
                                
                                linea = str(linea).replace("Línea", "").strip() if linea else ""
                                if linea.isdigit():
                                    linea = f"Línea {linea}"
                                elif not linea:
                                    linea = "Línea Desconocida"

                                # 2. EXTRAER ESTADO Y AFECTACIONES
                                est = celdas[1].get_text(strip=True).replace("Estado", "").strip()
                                afec = celdas[2].get_text(strip=True).replace("Estaciones afectadas", "").strip()
                                
                                # 3. EXTRAER INFORMACIÓN ADICIONAL
                                info_adicional = ""
                                if len(celdas) >= 4:
                                    info_adicional = celdas[3].get_text(strip=True).replace("Información adicional", "").strip()
                                
                                # 4. VALIDAR PROBLEMAS Y ARMAR MENSAJE
                                if "servicio regular" not in est.lower() or "ninguna" not in afec.lower():
                                    mensaje_linea = f"- *{linea}*: {est} | {afec}"
                                    
                                    if info_adicional and info_adicional.lower() != "ninguna":
                                        mensaje_linea += f"\n  ↳ _Info: {info_adicional}_"
                                        
                                    problemas.append(mensaje_linea)
                                    
                        except Exception as e:
                            logging.error(f"Se ignoró una fila corrupta: {str(e)}")
                            continue 
            
            return "Servicio Regular. Todo en orden." if not problemas else "*AFECTACIONES DETECTADAS:*\n" + "\n".join(problemas)

        except Exception as e:
            return f"Error en la extracción con ScraperAPI: {str(e)}"

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("ERROR CRÍTICO: Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"*REPORTE METROBÚS*\n\n{mensaje}",
            "parse_mode": "Markdown"
        }
        try:
            respuesta = requests.post(url, json=payload, timeout=15)
            respuesta.raise_for_status()
            logging.info("Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error al enviar Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
