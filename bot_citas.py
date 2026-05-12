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
    Analiza el siguiente texto y determina si contiene una cita (médica, dental, reunión, etc.), ya sea una solicitud, confirmación, recordatorio o asignación.
    
    Si contiene información de una cita, extrae la fecha y hora. 
    Responde ÚNICAMENTE con un objeto JSON que tenga las llaves "fecha" (formato YYYY-MM-DD) y "hora" (formato HH:MM).
    Si no hay hora especificada, asume las 09:00.
    Si la fecha es relativa (ej. "mañana", "el próximo martes"), calcúlala basándote en que hoy es {datetime.date.today().isoformat()}.
    
    Si el texto NO menciona ninguna cita clara, responde únicamente con {{}}.
    
    Texto: "{text}"
    """
    
    try:
        response = model.generate_content(prompt)
        clean_response = response.text.strip().replace('```json', '').replace('```', '')
        # Intentamos limpiar cualquier texto extra que Gemini pueda haber incluido
        if "{" in clean_response:
            clean_response = clean_response[clean_response.find("{"):clean_response.rfind("}")+1]
        data = json.loads(clean_response)
        return data if data and 'fecha' in data else None
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

    # Buscar correos de "Cita" de las últimas 24h
    query = '(cita OR citas) newer_than:1d'
    
    try:
        results = gmail.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
    except Exception as e:
        print(f"Error al listar mensajes de Gmail: {e}")
        return

    for msg_ref in messages:
        msg_id = msg_ref['id']
        try:
            msg = gmail.users().messages().get(userId='me', id=msg_id).execute()
            payload = msg.get('payload', {})
            
            def get_body(payload):
                """Extracts the body of the email, prioritizing plain text."""
                parts = payload.get('parts', [])
                body = ""
                
                # If no parts, body might be in the payload itself
                if not parts:
                    return payload.get('body', {}).get('data')
                
                # Priority: text/plain
                for part in parts:
                    if part.get('mimeType') == 'text/plain':
                        return part.get('body', {}).get('data')
                
                # Fallback: search recursively in parts
                for part in parts:
                    data = get_body(part)
                    if data:
                        return data
                return None

            body_data = get_body(payload)
            if body_data:
                body = base64.urlsafe_b64decode(body_data).decode('utf-8')
                appointment_data = extract_appointment_data(body, gemini_api_key)
                
                if appointment_data:
                    fecha = appointment_data.get('fecha')
                    hora = appointment_data.get('hora')
                    fecha_cita = datetime.datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
                    
                    # PREVENCIÓN DE DUPLICADOS
                    if is_duplicate(calendar, fecha_cita, msg_id):
                        print(f"Cita ya programada para el correo {msg_id}. Omitiendo...")
                        continue

                    event = {
                        'summary': f'Cita Médica: {msg["snippet"][:50]}',
                        'description': f'Correo ID: {msg_id}\n\nTexto original: {msg["snippet"]}',
                        'start': {'dateTime': fecha_cita.isoformat() + 'Z'},
                        'end': {'dateTime': (fecha_cita + datetime.timedelta(hours=1)).isoformat() + 'Z'},
                        'reminders': {
                            'useDefault': False,
                            'overrides': [
                                {'method': 'email', 'minutes': 24 * 60}, # 24 horas antes
                                {'method': 'email', 'minutes': 60},      # 1 hora antes
                                {'method': 'popup', 'minutes': 30},      # 30 minutos antes (notificación móvil/web)
                            ],
                        },
                    }
                    
                    calendar.events().insert(calendarId='primary', body=event).execute()
                    print(f"Nueva cita agendada: {fecha} {hora}")
        except Exception as e:
            print(f"Error con mensaje {msg_id}: {e}")

if __name__ == '__main__':
    main()
