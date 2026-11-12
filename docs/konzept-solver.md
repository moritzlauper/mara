# Konzept: Mixen per Vorhersage statt per Einzelrunde

Stand 12.11.2026.

## Befund

Mara probiert pro Runde eine kleine Änderung an einem Kanal, hört danach in Echtzeit, ob sie gewirkt hat, und nimmt sie sonst zurück. Dieser Ansatz verursacht beide Probleme, die schwachen Resultate und die lange Laufzeit.

- Ausbeute: Über alle 34 Run-Logs blieben von 427 Runden 132 Änderungen. Im letzten langen Lauf (lingottofieremara3, 34 Kanäle) waren es 2 von 19 Runden in 69 Minuten.
- Zu kleine Schritte: Der Master lag bei 400–640 Hz rund 5 dB über dem Ziel. Ein Schnitt von 1.5 dB auf einem von 23 Kanälen bewegt den Master um 0.1–0.3 dB. Das liegt unter der Messstreuung, also wird die Runde verworfen.
- Falscher Prüfmassstab: Geprüft wird gegen die Masking-Summe über alle Kanalpaare. Sie machte 7.2 von 10.4 Punkten aus und reagiert auf einen einzelnen Schnitt kaum.
- Lautheit bleibt liegen: Der Mix war 2.5 LU leiser als die Referenz. Der Limiter wurde nie angefasst, weil das Modell jede Runde Masking als Priorität wählte.
- Zeit: Eine Runde dauert etwa 3.5 Minuten. Jede Messung wartet auf den Loop-Anfang, und nach jedem Verwerfen wird neu gemessen.

## Neuer Ablauf

Die Taps liefern für jeden Kanal das Spektrum in 31 Bändern nach dem Fader. Daraus lässt sich berechnen, welchen Anteil jeder Kanal in jedem Band am Master hat. Die Wirkung von EQ und Fader auf den Master ist damit vorhersagbar, ohne jede Änderung einzeln anzuhören.

| Phase | Was passiert | Prüfung |
| --- | --- | --- |
| Master | Ein Solver verteilt die Abweichung zur Referenz auf die Kanäle, die sie verursachen. Hintergrundrollen geben mehr nach, der Kernbereich wichtiger Kanäle bleibt geschützt. Bass-Mono wird hier mit erledigt. | Paket anwenden, einmal messen, danach höchstens zweimal mit dem Rest nachlösen |
| Kanäle | Nur die stärksten Kollisionen, mit Schnitt, Fader oder Ducking auf dem Hintergrundkanal. Die Wirkung wird vorab aus den aufgezeichneten Frames simuliert. | Pro Kanalpaar; was nicht messbar hilft, wird einzeln zurückgenommen |
| Final | Höchstens zwei freie Modellrunden, danach Limiter und Clipper deterministisch in Richtung Referenz-Lautheit, zuletzt die Schlussmessung | Gegen den Originalzustand |

Kanäle, die im lautesten Teil nicht spielen, werden in höchstens zwei weiteren Passagen behandelt. Dort zählen nur Kollisionen.

Die Messung springt an den Loop-Anfang, statt auf den Umlauf zu warten. Eine doppelt geprüfte Messung dauert dann etwa 30 statt 70 Sekunden.

## Rolle des Modells

Das Modell schlägt nicht mehr 50- bis 100-mal einen Einzelschritt vor. Es bekommt einmal den Entwurf des Solvers mit dem Ziel des Nutzers und den Rollen. Es darf Züge streichen, abschwächen oder Kanäle ganz schützen («Rhodes warm lassen»), danach rechnet der Solver mit diesen Vorgaben neu. Am Schluss bleiben höchstens zwei freie Runden für das, was der Solver nicht abbildet, etwa Kompressoren, Sends und Resonanzen.

«Built-in rules» profitiert direkt, weil der Solver ohne Modell läuft.

## Entscheide

- Alle Kanäle stecken gleichzeitig in der Rechnung. Im Log steht pro Kanal, was entschieden wurde.
- Lautheit: Der Master darf höchstens 1 dB dichter (kleinere PLR) werden als die Referenz. Die Caps bleiben: Limiter +5 dB, Clipper +4 dB.
- Der alte Rundenablauf bleibt als Reserve (`--engine rounds`).

## Was bleibt

UI, Log-Format, Phasenanzeige, Checkpoints, Journal, A/B und alle Schutzregeln bleiben. Master-EQ höchstens 3 dB Schnitt, ausgeschaltete Geräte bleiben unberührt, automatisierte Parameter ebenso.

## Erwartung und Risiken

Für ein Set mit 34 Kanälen rechne ich mit 8–12 statt 70–90 Minuten und ein bis drei Modellaufrufen. Das ist geschätzt, nicht gemessen.

Die Vorhersage ist eine Näherung: Kanäle werden als unkorreliert addiert, Bus-Kompression und Limiter verändern die Tonalität darin nicht. Deshalb wird jedes Paket gemessen und bei Bedarf zurückgenommen. Getestet ist der Ablauf nur im Simulator, nicht in einer echten Live-Session.
