import os
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

MI_NUMERO = os.getenv("MI_NUMERO")
API_KEY = os.getenv("API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def obtener_estado_detallado(self) -> str:
        try:
            respuesta = requests.get(self.url, headers=self.headers, timeout=10)
            respuesta.raise_for_status()
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            problemas = []
            tablas = soup.find_all('table')
            
            for tabla in tablas:
                if 'Estaciones afectadas' in tabla.text:
                    for fila in tabla.find_all('tr')[1:]: # Saltamos el encabezado
                        celdas = fila.find_all('td')
                        if len(celdas) >= 3:
                            linea = celdas[0].get_text(strip=True)
                            est = celdas[1].get_text(strip=True).replace("Estado", "").strip()
                            afec = celdas[2].get_text(strip=True).replace("Estaciones afectadas", "").strip()
                            if "Servicio Regular" not in est or "Ninguna" not in afec:
                                problemas.append(f"• {linea}: {est} | {afec}")
            
            return "*Servicio Regular*" if not problemas else "⚠️ *AFECTACIONES:*\n" + "\n".join(problemas)
        except Exception as e:
            return f"Error al consultar: {str(e)}"

    def enviar_whatsapp(self, mensaje: str) -> None:
        if not MI_NUMERO or not API_KEY:
            logging.error("Faltan las credenciales en los Secrets de GitHub.")
            return
        
        msg_codificado = urllib.parse.quote(f"🚇 *REPORTE METROBÚS*\n\n{mensaje}")
        url = f"https://api.callmebot.com/whatsapp.php?phone={MI_NUMERO}&text={msg_codificado}&apikey={API_KEY}"
        requests.get(url)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_whatsapp(monitor.obtener_estado_detallado())