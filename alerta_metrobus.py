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
            return "Error: Falta la clave de ScraperAPI en los Secrets."

        problemas = []
        
        # Mapeo exacto basado en el orden de las filas de la tabla
        nombres_lineas = [
            "Línea 1",
            "Línea 2",
            "Línea 3",
            "Línea 4", # Fila 4
            "Línea 4", # Fila 5 (Excepción solicitada)
            "Línea 5",
            "Línea 6",
            "Línea 7"
        ]

        try:
            # 1. EVITAR CACHÉ: Agregamos un timestamp para forzar a ScraperAPI a buscar datos nuevos
            url_fresca = f"{self.url}?t={int(time.time())}"
            
            logging.info("Solicitando datos frescos al Metrobús (evitando caché)...")
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': url_fresca,
                'country_code': 'mx'
            }
            
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')
            
            tabla_servicio = None
            for tabla in tablas:
                if 'estado' in tabla.text.lower() and 'estaciones' in tabla.text.lower():
                    tabla_servicio = tabla
                    break

            if not tabla_servicio:
                logging.error("No se encontró la tabla de estados. Estructura de la web cambió?")
                return "Error: Estructura del sitio no reconocida."

            # Extraemos todas las filas ignorando el encabezado
            filas_datos = tabla_servicio.find_all('tr')[1:]
            
            # Contador para el mapeo de líneas
            indice_linea = 0
            
            for fila in filas_datos:
                celdas = fila.find_all('td')
                
                # Buscamos filas que tengan información real (Estado y Estaciones)
                if len(celdas) >= 3:
                    # Identificamos la línea por su posición
                    linea_nombre = nombres_lineas[indice_linea] if indice_linea < len(nombres_lineas) else f"Línea {indice_linea + 1}"
                    
                    # Extraemos los textos de cada columna
                    estado_txt = celdas[1].get_text(" ", strip=True)
                    afectadas_txt = celdas[2].get_text(" ", strip=True)
                    info_extra_txt = ""
                    
                    if len(celdas) >= 4:
                        info_extra_txt = celdas[3].get_text(" ", strip=True).replace("Información adicional", "").strip()

                    # Limpiamos para la lógica de comparación
                    est_lower = estado_txt.lower()
                    afec_lower = afectadas_txt.lower()

                    # Log para auditoría (esto aparecerá en GitHub Actions)
                    logging.info(f"Analizando {linea_nombre}: [{estado_txt}] - [{afectadas_txt}]")

                    # Lógica de detección de problemas:
                    # Si el estado no es "regular" O si hay estaciones que no sean "ninguna" o vacío
                    if "regular" not in est_lower or ("ninguna" not in afec_lower and afec_lower != ""):
                        reporte = f"- {linea_nombre}: {estado_txt} | {afectadas_txt}"
                        if info_extra_txt and info_extra_txt.lower() != "ninguna":
                            reporte += f" | Info: {info_extra_txt}"
                        problemas.append(reporte)
                    
                    indice_linea += 1

            return "Servicio Regular. Todo en orden." if not problemas else "AFECTACIONES DETECTADAS:\n" + "\n".join(problemas)

        except Exception as e:
            logging.error(f"Fallo en la extracción: {e}")
            return f"Error en la extracción: {str(e)}"

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"*REPORTE METROBÚS*\n\n{mensaje}",
            "parse_mode": "Markdown"
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            logging.info("Mensaje enviado a Telegram.")
        except Exception as e:
            logging.error(f"Fallo al enviar Telegram: {e}")

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
