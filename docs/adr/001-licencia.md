# ADR-001: Licencia del proyecto

**Estado**: Aceptada
**Fecha**: 2026-07-15

## Contexto

SynthDB se publicará como repositorio público en GitHub (plan de ejecución del
MVP, T0.1). Hace falta fijar la licencia antes del primer commit porque
condiciona el `LICENSE`, las cabeceras de los archivos y las expectativas de
quien contribuya.

Las dos candidatas naturales para un proyecto de herramientas de desarrollador
en Python son MIT y Apache-2.0: ambas son permisivas, compatibles entre sí y
ampliamente aceptadas en entornos corporativos.

## Decisión

Apache License 2.0.

Frente a MIT, Apache-2.0 añade una concesión de patente explícita (§3) y la
obligación de señalar los cambios en archivos modificados (§4b), lo que reduce
fricción legal para adopción en entornos empresariales sin restar
permisividad práctica para uso individual.

## Consecuencias

- `LICENSE` en la raíz con el texto completo de Apache-2.0.
- Los archivos nuevos no necesitan cabecera de copyright por archivo salvo
  que se decida lo contrario más adelante; el aviso de la raíz basta para el
  MVP.
- Cualquier dependencia añadida al proyecto debe ser compatible con
  Apache-2.0 (MIT, BSD, Apache-2.0 lo son; GPL/AGPL no deben incorporarse a
  `src/`).
- El README debe declarar la licencia y enlazar a este ADR.
