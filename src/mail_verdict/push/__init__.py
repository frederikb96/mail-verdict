"""
Web Push: the half of alert delivery that reaches a device with no
MailVerdict page open. vapid.py owns this server's signing identity;
send.py sends an alert to every subscription that wants it. See alerts/
for the alert record itself and the in-app path that needs none of this.
"""
