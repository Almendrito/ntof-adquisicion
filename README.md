# ntof-adquisicion

Captura de señales de los detectores del diagnóstico nToF de Llampüdkeñ y análisis de sus pulsos.

## Osciloscopio Tektronix TDS 684A (disparos de Llampüdkeñ)

| Archivo | Qué hace |
| --- | --- |
| `captura_tds684a.py` | Lee el osciloscopio por GPIB y guarda un CSV por disparo con el número, el voltaje del PMT, el gas y el detector conectado a cada canal |
| `analisis_pulsos.py` | Abre esos CSV y mide línea base, amplitud, ancho a media altura, subida, bajada, integral y carga. Compara formas entre disparos y grafica tendencias |

Necesita Python con `pyvisa`, `numpy` y `matplotlib`, más los drivers NI-488.2 y NI-VISA para el adaptador GPIB.

```powershell
python captura_tds684a.py
python analisis_pulsos.py
```

## Osciloscopio Rigol MHO984 (mediciones con fuentes gamma)

Carpeta `rigol_mho984/`, de abril a junio de 2026.

| Archivo | Qué hace |
| --- | --- |
| `rigol_mho984_diagnostic.py` | Revisa por LAN o USB qué modos de adquisición ofrece el osciloscopio y cuántos pulsos por segundo alcanza |
| `rigol_mho984_gui.py` | Interfaz con tres pestañas: diagnóstico, captura de pulsos del PMT a disco y visor de sesiones guardadas |
| `manual.py` | Captura simple por LAN que guarda los pulsos en un archivo `.h5` |
| `visor_manual.py`, `visor_manual_v2.py` | Visores de esos `.h5`: pulso individual, histograma de alturas o integrales y forma promedio |

Además de lo anterior necesita `h5py`.

## Datos

Los CSV y los `.h5` no se suben a GitHub. Se quedan en OneDrive.
