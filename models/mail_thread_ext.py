# -*- coding: utf-8 -*-
from odoo import models


class MailThreadInboxOnly(models.AbstractModel):
    """Notifications de transfert : boîte de réception Odoo uniquement.

    DEMANDE UTILISATEUR : les notifications de transfert du dashboard restent
    dans Odoo (Discussion), jamais par email — quelle que soit la préférence
    « Notification » de chaque utilisateur (la plupart des responsables sont
    réglés sur « par email »).

    Odoo choisit le canal destinataire par destinataire dans
    _notify_get_recipients ('notif' = 'inbox' ou 'email') et message_notify
    n'a aucun paramètre pour l'imposer : le contrôleur pose donc le drapeau de
    contexte mavie_notify_inbox_only, et seul ce cas est modifié ici.
    """
    _inherit = 'mail.thread'

    def _notify_get_recipients(self, message, msg_vals, **kwargs):
        recipients = super()._notify_get_recipients(message, msg_vals, **kwargs)
        if not self.env.context.get('mavie_notify_inbox_only'):
            return recipients
        # Un partenaire sans utilisateur interne n'a pas de boîte de
        # réception Odoo : il ne pourrait être joint que par email, il est
        # donc écarté plutôt que de recevoir un mail.
        return [
            dict(rdata, notif='inbox')
            for rdata in recipients
            if (rdata.get('uid') or rdata.get('type') == 'user') and not rdata.get('share')
        ]
