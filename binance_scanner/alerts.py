from scanner.alerts import Alerts as SharedAlerts


class Alerts(SharedAlerts):
    def emit(self,event,**fields):
        fields['exchange']='BINANCE'
        super().emit(event,**fields)
