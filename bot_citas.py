import os
import datetime
import json
import base64
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import google.generativeai as genai

# Configuración de alcances
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/calendar']

def extract_appointment_data(text, api_key):
    """Uses Gemini to extract structured date and time from natural language text."""
    if not api_key:
        print("GEMINI_API_KEY no proporcionada.")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"""
    Analiza el siguiente texto y determina si contiene una cita, webinar, reunión o encuentro programado.
    
    Presta especial atención a frases como "Nuestra cita es", "La cita es el", "Fecha:", "Mayo 13 de 2026".
    
    Si contiene información de una cita, extrae la fecha y hora. 
    Responde ÚNICAMENTE con un objeto JSON que tenga las llaves "fecha" (formato YYYY-MM-DD) y "hora" (formato HH:MM).
    Si no hay hora especificada, asume las 09:00.
    Si la fecha es relativa (ej. "mañana"), calcúlala basándote en que hoy es {datetime.date.today().isoformat()}.
    
    Si el texto NO menciona ninguna cita clara, responde únicamente con {{}}.
    
    Texto: "{text[:4000]}" # Limitamos para no exceder tokens
    """
    
    try:
        response = model.generate_content(prompt)
        text_response = response.text.strip()
        print(f"DEBUG: Respuesta de Gemini: {text_response}")
        
        # Limpieza robusta del JSON
        if "{" in text_response:
            json_str = text_response[text_response.find("{"):text_response.rfind("}")+1]
            data = json.loads(json_str)
            if data and 'fecha' in data:
                return data
        return None
    except Exception as e:
        print(f"Error procesando con Gemini: {e}")
        return None

def is_duplicate(calendar, start_time, msg_id):
    """Checks if an event for this message or at this time already exists."""
    # Buscamos eventos en una ventana de 2 horas alrededor de la cita
    time_min = (start_time - datetime.timedelta(hours=1)).isoformat() + 'Z'
    time_max = (start_time + datetime.timedelta(hours=1)).isoformat() + 'Z'
    
    events_result = calendar.events().list(calendarId='primary', timeMin=time_min, 
                                          timeMax=time_max, singleEvents=True).execute()
    events = events_result.get('items', [])
    
    for event in events:
        # Verificamos si el ID del correo está en la descripción
        if msg_id in event.get('description', ''):
            print(f"DEBUG: Duplicado detectado por ID de mensaje {msg_id}")
            return True
        # Si ya hay algo a esa misma hora, también lo consideramos duplicado para evitar traslapes
        if event['start'].get('dateTime') == start_time.isoformat() + 'Z':
             print(f"DEBUG: Duplicado detectado por hora {start_time}")
             return True
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

    # Buscamos de forma muy amplia: palabra cita o remitente Ruta N, en los últimos 7 días
    query = '("cita" OR "citas" OR "Ruta N" OR "webinar") newer_than:7d'
    print(f"DEBUG: Ejecutando búsqueda en Gmail con query: {query}")
    
    try:
        results = gmail.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
        print(f"DEBUG: Encontrados {len(messages)} mensajes potenciales.")
    except Exception as e:
        print(f"Error al listar mensajes de Gmail: {e}")
        return

    for msg_ref in messages:
        msg_id = msg_ref['id']
        try:
            msg = gmail.users().messages().get(userId='me', id=msg_id).execute()
            subject = "Sin Asunto"
            for header in msg.get('payload', {}).get('headers', []):
                if header['name'] == 'Subject':
                    subject = header['value']
            
            print(f"\nDEBUG: Procesando mensaje ID: {msg_id} | Asunto: {subject}")
            
            payload = msg.get('payload', {})
            
            def get_body(payload):
                """Extracts the body of the email, prioritizing plain text."""
                mime_type = payload.get('mimeType')
                parts = payload.get('parts', [])
                
                if not parts:
                    return payload.get('body', {}).get('data')
                
                # Prioridad 1: buscar texto plano en las partes inmediatas
                for part in parts:
                    if part.get('mimeType') == 'text/plain':
                        return part.get('body', {}).get('data')
                
                # Prioridad 2: buscar recursivamente
                for part in parts:
                    data = get_body(part)
                    if data:
                        return data
                return None

            body_data = get_body(payload)
            if body_data:
                body = base64.urlsafe_b64decode(body_data).decode('utf-8')
                print(f"DEBUG: Cuerpo extraído ({len(body)} caracteres). Enviando a Gemini...")
                appointment_data = extract_appointment_data(body, gemini_api_key)
                
                if appointment_data:
                    fecha = appointment_data.get('fecha')
                    hora = appointment_data.get('hora')
                    print(f"DEBUG: Gemini detectó cita para {fecha} a las {hora}")
                    fecha_cita = datetime.datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
                    
                    # PREVENCIÓN DE DUPLICADOS
                    if is_duplicate(calendar, fecha_cita, msg_id):
                        print(f"Omitiendo: Cita ya programada para {msg_id} o a esa misma hora.")
                        continue

                    event = {
                        'summary': f'Cita/Webinar: {subject[:50]}',
                        'description': f'Correo ID: {msg_id}\n\nResumen: {msg["snippet"]}',
                        'start': {'dateTime': fecha_cita.isoformat() + 'Z'},
                        'end': {'dateTime': (fecha_cita + datetime.timedelta(hours=1)).isoformat() + 'Z'},
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
                    print(f"✅ EXITO: Nueva cita agendada: {fecha} {hora}")
                else:
                    print("DEBUG: Gemini no encontró una cita válida en este correo.")
        except Exception as e:
            print(f"Error con mensaje {msg_id}: {e}")
        except Exception as e:
            print(f"Error con mensaje {msg_id}: {e}")

if __name__ == '__main__':
    main()
