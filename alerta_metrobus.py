import os
import logging
import requests
from bs4 import BeautifulSoup

# Credenciales desde GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_detallado(self) -> str:
        if not SCRAPER_API_KEY:
            return "❌ Error: Falta la clave de ScraperAPI en los Secrets."

        try:
            # Usamos ScraperAPI con renderizado JS, pero SIN el premium que causaba el Error 500
            params = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'country_code': 'mx',
                'render': 'true' 
            }
            
            logging.info("Consultando página a través de ScraperAPI...")
            # Damos 90 segundos porque el renderizado JS en servidores proxy toma tiempo
            respuesta = requests.get('http://api.scraperapi.com/', params=params, timeout=90)
            respuesta.raise_for_status()

            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')
            problemas = []
            tabla_encontrada = False

            for tabla in tablas:
                # Buscamos la tabla correcta
                if 'estaciones afectadas' in tabla.text.lower():
                    tabla_encontrada = True
                    filas = tabla.find_all('tr')
                    
                    for fila in filas[1:]: # Saltamos el encabezado
                        # Buscamos tanto 'td' como 'th' por si la línea 1 está formateada diferente
                        celdas = fila.find_all(['td', 'th']) 
                        if len(celdas) >= 3:
                            # get_text con separator ayuda a limpiar etiquetas ocultas de móviles
                            linea = celdas[0].get_text(separator=" ", strip=True)
                            est = celdas[1].get_text(separator=" ", strip=True)
                            afec = celdas[2].get_text(separator=" ", strip=True)
                            
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                problemas.append(f"- {linea}: {est} | Cerradas: {afec}")
            
            # Candado de seguridad: Si no vio la tabla, te avisa en lugar de dar un falso positivo
            if not tabla_encontrada:
                logging.warning("No se encontró la tabla de afectaciones. La estructura web cambió.")
                return "⚠️ El bot logró conectarse, pero no pudo leer la tabla de afectaciones."
            
            return "✅ Servicio Regular" if not problemas else "⚠️ AFECTACIONES DETECTADAS:\n" + "\n".join(problemas)

        except requests.exceptions.RequestException as e:
            return f"❌ Error de red (ScraperAPI): {str(e)}"
        except Exception as e:
            return f"❌ Error extrayendo datos: {str(e)}"

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
            logging.error(f"Fallo al conectar con Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
