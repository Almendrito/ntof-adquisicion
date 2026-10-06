import pyvisa
import numpy as np
import h5py 
import time 

direccion_IP = f"169.254.179.2"
VISA_ADDRESS = f"TCPIP::{direccion_IP}::INSTR"

def test_conexion():

    rm = pyvisa.ResourceManager()
    osc = None 

    try:
        osc = rm.open_resource(VISA_ADDRESS)
        osc.timeout = 2000

        idn = osc.query("*IDN?")
        print(f"Conectado a: {idn}")
        print(f"Configurar")
        osc.write(":CHANnel2:DISPlay ON")
        osc.write(":CHANnel2:SCALe 0.2")
        osc.write(":TIMebase:MAIN:SCALe 10e-9")
        osc.write(f":WAVeform:SOURce CHANnel2")
        osc.write(f":WAVeform:FORMat BYTE")
        osc.write(f":WAVeform:MODE NORMal")

        osc.query("*OPC?")
        print("Se configuro :)")

    except pyvisa.errors.VisaIOError as e:
        print(f"Error de VISA: Revisa la IP o la conexión física.\nDetalle: {e}")
    except Exception as e:
        print(f"Error inesperado: {e}")
    return osc
    
def toma_datos(osc):

    #osc.write(f":WAVeform:SOURce CHANnel{canal}"
    try:
        datos_crudos = osc.query_binary_values(
            ":WAVeform:DATA?",
            datatype = "B",
            container=np.array,
            header_fmt = "ieee"
        )
        return datos_crudos
    except Exception as e:
        print(f"ERROR D: {e}")
        return None

def apagar_OS(osc):
    osc.close()
    print("cerrao")

def limpieza(lista_cruda,umbral_ruido = 7):

    lista_limpia = []
    pulso_anterior = None
    
    pulsos_descartados_redundancia = 0
    pulsos_descartados_ruido = 0
    
    for pulso_actual in lista_cruda:
        # 1. Filtro de Redundancia: Si el hardware no actualizó la pantalla
        if pulso_anterior is not None and np.array_equal(pulso_actual, pulso_anterior):
            pulsos_descartados_redundancia += 1
            continue # Saltamos a la siguiente iteración
            
        # 2. Filtro de Ruido: Evaluamos la variación Peak-to-Peak (Max - Min)
        amplitud_maxima = np.ptp(pulso_actual)
        
        if amplitud_maxima < umbral_ruido:
            pulsos_descartados_ruido += 1
            pulso_anterior = pulso_actual # Actualizamos para no comparar el siguiente con un pulso fantasma
            continue # Descartamos por ser solo ruido
            
        # Si pasa ambos filtros, es un pulso real y válido
        lista_limpia.append(pulso_actual)
        pulso_anterior = pulso_actual
            
    print(f"Limpieza finalizada:")
    print(f" - Capturas brutas: {len(lista_cruda)}")
    print(f" - Descartados por hardware lento (redundancia): {pulsos_descartados_redundancia}")
    print(f" - Descartados por ser solo ruido electrónico: {pulsos_descartados_ruido}")
    print(f" - Eventos VÁLIDOS conservados: {len(lista_limpia)}")
    
    return lista_limpia

if __name__ == "__main__":
    pulsos = 0
    osc = test_conexion()
    if osc is not None:
        tiempo_medicion = 3600
        tiempo_inicio = time.time()
        lista_de_pulsos = []
        try:

            while (time.time() - tiempo_inicio) < tiempo_medicion:
                pulso = toma_datos(osc)
                
                if pulso is not None:
                    lista_de_pulsos.append(pulso)
                    # Feedback rápido en consola
                    print(f"Pulsos capturados: {len(lista_de_pulsos)}", end="\r")
            
            print(f"\n\nTiempo cumplido. Guardando {len(lista_de_pulsos)} pulsos...")

        except KeyboardInterrupt:
            print("\nMedición cancelada por el usuario.")
        finally:
            osc.close()

    pulsos_validos = limpieza(lista_de_pulsos)
    nombre_archivo = "CO1Hrmedicion_temporal.h5"
with h5py.File(nombre_archivo, "w") as h5f:
    grupo = h5f.create_group("pulsos_crudos")
    for i, p in enumerate(pulsos_validos):
        grupo.create_dataset(f"pulso_{i}", data=p)

print(f"Archivo {nombre_archivo} creado exitosamente.")