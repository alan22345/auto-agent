"""Simple Flask REST API for user management."""

from flask import Flask, jsonify, request

from models import db, User
from routes import user_bp

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
app.register_blueprint(user_bp, url_prefix="/api")


@app.before_request
def create_tables():
    db.create_all()


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
