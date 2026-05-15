import os
import logging
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def obtener_estado_detallado(self) -> str:
        problemas = []
        try:
            logging.info("Consultando la página del Metrobús...")
            respuesta = requests.get(self.url, headers=self.headers, timeout=15)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')

            for tabla in tablas:
                # Buscamos la tabla correcta
                if 'estaciones afectadas' in tabla.text.lower():
                    # Brincamos la fila 0 porque son los títulos de las columnas
                    for fila in tabla.find_all('tr')[1:]:
                        celdas = fila.find_all('td')
                        
                        # Si la fila tiene al menos 3 columnas...
                        if len(celdas) >= 3:
                            # 1. INTENTAR EXTRAER EL NOMBRE DE LA LÍNEA
                            linea = celdas[0].get_text(strip=True)
                            
                            # Si el texto está vacío, buscamos dentro de la imagen/logotipo
                            if not linea:
                                img = celdas[0].find('img')
                                if img:
                                    # Intentamos obtener el atributo "alt" o "title" de la imagen
                                    linea = img.get('alt') or img.get('title') or "Línea Desconocida"
                                    linea = linea.strip()
                            
                            # Limpiamos el texto invisible de responsive ("Línea") si se cuela
                            linea = linea.replace("Línea", "").strip()
                            if linea.isdigit():
                                linea = f"Línea {linea}" # Lo formateamos bonito si solo saca el número
                            elif not linea:
                                linea = "Línea Desconocida"

                            # 2. EXTRAER ESTADO Y AFECTACIONES
                            est = celdas[1].get_text(strip=True).replace("Estado", "").strip()
                            afec = celdas[2].get_text(strip=True).replace("Estaciones afectadas", "").strip()
                            
                            # 3. EXTRAER INFORMACIÓN ADICIONAL (Cuarta columna)
                            info_adicional = ""
                            if len(celdas) >= 4:
                                info_adicional = celdas[3].get_text(strip=True).replace("Información adicional", "").strip()
                            
                            # Validar si hay afectación para añadir a la lista
                            if "servicio regular" not in est.lower() or "ninguna" not in afec.lower():
                                # Construimos el mensaje final para esta línea
                                mensaje_linea = f"- *{linea}*: {est} | {afec}"
                                
                                # Si hay información extra en la web, la pegamos abajo
                                if info_adicional and info_adicional.lower() != "ninguna":
                                    mensaje_linea += f"\n  ↳ _Info: {info_adicional}_"
                                    
                                problemas.append(mensaje_linea)
            
            return "Servicio Regular. Todo en orden." if not problemas else "⚠️ *AFECTACIONES DETECTADAS:*\n" + "\n".join(problemas)

        except Exception as e:
            return f"Error en la extracción: {str(e)}"

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("ERROR CRÍTICO: Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚇 *REPORTE METROBÚS*\n\n{mensaje}",
            "parse_mode": "Markdown" # Markdown permite poner negritas y cursivas en Telegram
        }
        try:
            respuesta = requests.post(url, json=payload, timeout=15)
            respuesta.raise_for_status()
            logging.info("Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error al enviar Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://incidentesmovilidad.cdmx.gob.mx/public/bandejaEstadoServicio.xhtml?idMedioTransporte=mb")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
