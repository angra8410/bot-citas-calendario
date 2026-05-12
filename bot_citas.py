import os
import datetime
import json
import base64
import time
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google import genai

# Configuración de alcances
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/calendar']

def extract_appointment_data(text, api_key):
    """Uses the new Google GenAI SDK with multiple fallbacks for model and API version."""
    if not api_key:
        print("GEMINI_API_KEY no proporcionada.")
        return None

    # Priorizamos 2.5-flash que es el que funcionó en el log anterior
    configs = [
        {'model': 'gemini-2.5-flash', 'version': 'v1'},
        {'model': 'gemini-2.0-flash', 'version': 'v1'},
        {'model': 'gemini-flash-latest', 'version': 'v1'},
    ]

    prompt = f"""
    Analiza el siguiente texto de un correo y extrae la fecha y hora del PRÓXIMO o SIGUIENTE evento (cita, webinar, reunión, encuentro).
    
    IMPORTANTE: 
    1. Ignora fechas pasadas (como menciones de webinars que ya ocurrieron). 
    2. Busca frases como "Nuestra cita es", "próximo encuentro", "Fecha:", "Mayo 13 de 2026".
    3. Responde ÚNICAMENTE con un objeto JSON con las llaves "fecha" (YYYY-MM-DD) y "hora" (HH:MM en formato 24h).
    4. Si hay un rango de horas (ej. 6:00 pm a 7:00 pm), usa la hora de inicio (18:00).
    5. Si el texto no menciona ninguna cita futura clara, responde únicamente con {{}}.
    
    Hoy es {datetime.date.today().isoformat()}.
    
    Texto del correo:
    "{text[:5000]}"
    """

    for config in configs:
        try:
            client = genai.Client(api_key=api_key, http_options={'api_version': config['version']})
            response = client.models.generate_content(
                model=config['model'],
                contents=prompt
            )
            
            text_response = response.text.strip()
            print(f"DEBUG: Éxito con {config['model']} ({config['version']})")
            
            if "{" in text_response:
                json_str = text_response[text_response.find("{"):text_response.rfind("}")+1]
                data = json.loads(json_str)
                if data and 'fecha' in data and 'hora' in data:
                    return data
            return None
        except Exception as e:
            if "404" in str(e):
                continue 
            if "429" in str(e):
                print(f"DEBUG: Cuota agotada para {config['model']}. Probando siguiente...")
                continue
            print(f"Error procesando con {config['model']} ({config['version']}): {e}")
            
    return None

def is_duplicate(calendar, start_time, msg_id):
    """Checks if an event for this message or at this time already exists."""
    # Buscamos eventos en una ventana de 2 horas alrededor de la cita
    time_min = (start_time - datetime.timedelta(hours=1)).isoformat() + 'Z'
    time_max = (start_time + datetime.timedelta(hours=1)).isoformat() + 'Z'
    
    try:
        events_result = calendar.events().list(calendarId='primary', timeMin=time_min, 
                                              timeMax=time_max, singleEvents=True).execute()
        events = events_result.get('items', [])
        
        for event in events:
            # Verificamos si el ID del correo está en la descripción
            if msg_id in event.get('description', ''):
                print(f"DEBUG: Duplicado detectado por ID de mensaje {msg_id}")
                return True
            # Si ya hay algo a esa misma hora, también lo consideramos duplicado
            event_start = event['start'].get('dateTime') or event['start'].get('date')
            if event_start and start_time.isoformat() in event_start:
                 print(f"DEBUG: Duplicado detectado por hora {start_time}")
                 return True
    except Exception as e:
        print(f"Error verificando duplicados: {e}")
    return False

def main():
    creds_info = os.environ.get('GOOGLE_USER_CREDS')
    if not creds_info:
        print("Error: GOOGLE_USER_CREDS no configurado.")
        return
        
    try:
        creds_dict = json.loads(creds_info)
        creds = Credentials.from_authorized_user_info(info=creds_dict, scopes=SCOPES)
    except Exception as e:
        print(f"Error cargando credenciales: {e}")
        return

    gemini_api_key = os.environ.get('GEMINI_API_KEY')
    
    gmail = build('gmail', 'v1', credentials=creds)
    calendar = build('calendar', 'v3', credentials=creds)

    # Búsqueda amplia
    query = '("cita" OR "citas" OR "Ruta N" OR "webinar" OR "encuentro") newer_than:7d'
    print(f"DEBUG: Buscando correos con query: {query}")
    
    try:
        results = gmail.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
        print(f"DEBUG: Se encontraron {len(messages)} mensajes potenciales.")
    except Exception as e:
        print(f"Error al listar mensajes de Gmail: {e}")
        return

    for msg_ref in messages:
        msg_id = msg_ref['id']
        try:
            # Pequeña pausa para no saturar la API de Gemini (Free Tier)
            time.sleep(2)
            
            msg = gmail.users().messages().get(userId='me', id=msg_id).execute()
            subject = "Sin Asunto"
            headers = msg.get('payload', {}).get('headers', [])
            for header in headers:
                if header['name'] == 'Subject':
                    subject = header['value']
            
            print(f"\n--- Analizando: {subject} (ID: {msg_id}) ---")
            
            def get_body(payload):
                """Recursively extracts the body, prioritizing plain text, then HTML."""
                parts = payload.get('parts', [])
                if not parts:
                    return payload.get('body', {}).get('data')
                for part in parts:
                    if part.get('mimeType') == 'text/plain':
                        return part.get('body', {}).get('data')
                for part in parts:
                    if part.get('mimeType') == 'text/html':
                        return part.get('body', {}).get('data')
                for part in parts:
                    data = get_body(part)
                    if data:
                        return data
                return None

            body_data = get_body(msg.get('payload', {}))
            if body_data:
                try:
                    body = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')
                    appointment_data = extract_appointment_data(body, gemini_api_key)
                    
                    if appointment_data:
                        fecha = appointment_data.get('fecha')
                        hora = appointment_data.get('hora')
                        print(f"DEBUG: Datos extraídos: Fecha={fecha}, Hora={hora}")
                        
                        try:
                            fecha_cita = datetime.datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
                        except Exception as parse_err:
                            print(f"Error al parsear fecha/hora '{fecha} {hora}': {parse_err}")
                            continue
                        
                        if is_duplicate(calendar, fecha_cita, msg_id):
                            print(f"⚠️ Omitido: Ya existe una cita programada para este correo o a esta hora.")
                            continue

                        event = {
                            'summary': f'Evento: {subject[:50]}',
                            'description': f'Correo ID: {msg_id}\n\nSnippet: {msg["snippet"]}',
                            'start': {'dateTime': fecha_cita.isoformat(), 'timeZone': 'America/Bogota'},
                            'end': {'dateTime': (fecha_cita + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Bogota'},
                            'reminders': {
                                'useDefault': False,
                                'overrides': [
                                    {'method': 'email', 'minutes': 24 * 60},
                                    {'method': 'email', 'minutes': 60},
                                    {'method': 'popup', 'minutes': 30},
                                ],
                            },
                        }
                        
                        calendar.events().insert(calendarId='primary', body=event).execute()
                        print(f"✅ ÉXITO: Cita agendada para {fecha} {hora}")
                    else:
                        print("DEBUG: Gemini no detectó una cita válida en el contenido.")
                except Exception as decode_err:
                    print(f"Error decodificando cuerpo: {decode_err}")
            else:
                print("DEBUG: No se pudo extraer el cuerpo del mensaje.")
        except Exception as e:
            print(f"Error con mensaje {msg_id}: {e}")

if __name__ == '__main__':
    main()
