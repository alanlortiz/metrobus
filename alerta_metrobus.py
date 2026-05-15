import os
import logging
import requests
from bs4 import BeautifulSoup
import urllib3

# Ocultamos advertencias si el certificado de seguridad del gobierno falla
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_linea_1(self) -> str:
        try:
            logging.info("Consultando directamente el portal del Metrobús...")
            # Usamos cabeceras para parecer un navegador normal
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            }
            
            # verify=False ayuda a que no falle si la página de gobierno tiene problemas de seguridad básicos
            respuesta = requests.get(self.url, headers=headers, timeout=20, verify=False)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')
            
            for tabla in tablas:
                # Buscamos la tabla correcta
                if 'Estaciones afectadas' in tabla.text:
                    filas = tabla.find_all('tr')
                    
                    # Recorremos cada fila saltando los encabezados
                    for fila in filas[1:]:
                        celdas = fila.find_all('td')
                        if len(celdas) >= 3:
                            # La columna 1 suele tener la imagen del número de línea.
                            # Extraemos todo (texto, nombres de imagen, etc) para asegurar que leemos la "1"
                            identificador = celdas[0].get_text(strip=True)
                            img = celdas[0].find('img')
                            if img:
                                identificador += str(img.get('src', '')) + str(img.get('alt', ''))
                            
                            # Si detectamos un "1" en la primera columna, es la Línea 1
                            if '1' in identificador:
                                estado = celdas[1].get_text(strip=True).replace("Estado", "").strip()
                                afectadas = celdas[2].get_text(strip=True).replace("Estaciones afectadas", "").strip()
                                
                                info = ""
                                if len(celdas) >= 4:
                                    info = celdas[3].get_text(strip=True).replace("Información adicional", "").strip()
                                
                                # LÓGICA SOLICITADA:
                                if "servicio regular" in estado.lower():
                                    return "✅ Servicio Regular"
                                else:
                                    mensaje = f"⚠️ LÍNEA 1: {estado}"
                                    if afectadas and "ninguna" not in afectadas.lower():
                                        mensaje += f"\nCerradas: {afectadas}"
                                    if info:
                                        mensaje += f"\nInfo: {info}"
                                    return mensaje
                                    
            return "❌ Error: La página cargó pero no encontré la información de la Línea 1."

        except Exception as e:
            return f"❌ Error de red al consultar la página: {str(e)}"

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("Faltan credenciales de Telegram.")
            exit(1)
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}
        
        try:
            requests.post(url, json=payload, timeout=15)
            logging.info("Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Fallo al enviar Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    reporte = monitor.obtener_estado_linea_1()
    monitor.enviar_telegram(reporte)
