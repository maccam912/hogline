from sqlalchemy import select

from hogline.orchestration import recording_campaigns as campaigns


def register_campaigns(app, db):
    @app.post("/api/recording-campaigns/preview")
    def preview(body: campaigns.CampaignRequest):
        return campaigns.preview(db, body)

    @app.post("/api/recording-campaigns")
    def start(body: campaigns.CampaignStart):
        return campaigns.start(db, body)

    @app.get("/api/recording-campaigns")
    def listing():
        with db.snapshot_session() as session:
            return {
                "campaigns": [
                    campaigns.view(row) for row in session.scalars(select(campaigns.RecordingCampaign))
                ]
            }

    @app.post("/api/recording-campaigns/{identifier}/control")
    def control(identifier: str, body: campaigns.CampaignControl):
        return campaigns.control(db, identifier, body.action)
