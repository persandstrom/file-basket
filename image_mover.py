#!/usr/bin/env python

import MediaInfoDLL
import time
import ConfigParser
import os
import threading
import logging
import datetime 
import dateutil.parser
import dateutil.tz
import tempfile
from PIL import Image
import  MediaInfoDLL
import pyinotify
import subprocess
import Queue
import external
import uuid
import re

class ImageMover(pyinotify.ProcessEvent):
    def __init__(self):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self._parse_config(os.path.join(self.path, 'image_mover.cfg'))
        logging.basicConfig(filename = os.path.join(self.path, self.log_file), level =int(self.log_level), format='%(asctime)s %(message)s' )
        logging.info('Application started')
        self._expand_home_directory_in_config()
        self.lock = threading.Lock()
        self.queue = Queue.Queue()

    def _parse_config(self, config_file_path):
        parser = ConfigParser.ConfigParser()
        parser.read(config_file_path)
        options = parser.options('Options')
        for option in options:
            self.__dict__[option] = parser.get('Options', option)

    def _expand_home_directory_in_config(self):
        home_path = os.path.expanduser('~')
        self.from_path = self.from_path.replace('~', home_path)
        self.to_path = self.to_path.replace('~', home_path)

    def start(self):
        self._validate()
        self._start_observing_from_directory()
    
    def _validate(self):
        if not os.path.isdir(self.from_path):
            raise Exception('From directory, %s, not found' % self.from_path)
        if not os.path.isdir(self.to_path):
            raise Exception('To directory, %s, not found' % self.to_path)
    
    def _start_observing_from_directory(self):
        wm = pyinotify.WatchManager()
        mask = pyinotify.IN_CLOSE_WRITE | pyinotify.IN_CLOSE_NOWRITE | pyinotify.IN_MOVED_TO 
        notifier = pyinotify.ThreadedNotifier(wm, self)		
        notifier.start()
        wm.add_watch(self.from_path, mask, rec=False)
        try:
            while True:
                if not self.queue.empty():
                    self._move_file(self.queue.get())
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info('Application ending by request from user')
        notifier.stop()

    def process_IN_MOVED_TO(self, event):
        logging.debug('File moved to observed folder')
        self.queue.put(os.path.join(event.path, event.name))

    def process_IN_CLOSE_WRITE(self, event):
        logging.debug('File copied to observed folder')
        self.queue.put(os.path.join(event.path, event.name))

    def _move_file(self, source_file):
        try:
            time.sleep(1)
            with self.lock:
                file_name, extension = os.path.splitext(source_file)
                new_name = None
		if extension.lower() == '.mp4':
                    new_name = self._get_new_name_from_video_metadata(source_file)
		elif extension.lower() == '.mov' or extension.lower() == '.3gp':
		    converted_name = '{}.mp4'.format(file_name)
                    with tempfile.NamedTemporaryFile(delete = False, suffix = '.mp4') as temp_file:
		        external.call('ffmpeg', '-i', source_file, '-vcodec', 'copy', '-acodec', 'copy', '-map_metadata', '0', '-y', temp_file.name).assert_status(0)
                        os.rename(temp_file.name, converted_name)
                        os.remove(source_file)
		    return
                else:
                    new_name = self._get_new_name_from_exif(source_file)
                if not new_name:
                    new_name = os.path.basename(source_file)
                    # new_name = self._get_new_name_from_date_created(source_file)
                    destination = os.path.join(self.from_path, 'failed', new_name)
                else:
                    destination = os.path.join(self.to_path, new_name)
                if not os.path.isdir(os.path.dirname(destination)):	
                    os.makedirs(os.path.dirname(destination))
                if os.path.isfile('{}{}'.format(destination, extension)):
                    raise Exception("Desitnation file, {}, already exists, could not rename {}".format('{}{}'.format(destination, extension), source_file))
                if extension.lower() == '.mp4':
                    logging.debug("Creating low res version of {}".format(source_file))
                    size = self._get_video_size(source_file)
                    scale = 'scale=320:-2' if size[0] < size[1] else 'scale=-2:320'
                    external.call('ffmpeg', '-i', source_file, '-map_metadata', '0', '-vf', scale, '{}_{}'.format(destination, extension)) 

                logging.debug("Moved from {} to {}".format(source_file, '{}{}'.format(destination, extension)))
                os.rename(source_file, '{}{}'.format(destination, extension))

        except Exception as ex:
            logging.warning(ex.message)
    
    def _get_new_name_from_exif(self, source_file):
        try:
            image = Image.open(source_file)
            date_string = image._getexif()[36867].replace(':', '').replace(':', '')
            shot_date = dateutil.parser.parse(date_string)
            return shot_date.strftime(self.file_format)
        except Exception as ex:
            logging.warning('%s: %s' % (source_file, ex.message))
            return None

    def _get_new_name_from_video_metadata(self, source_file):	
        try:
            mi = MediaInfoDLL.MediaInfo()
            mi.Open(source_file)
            shot_date = None
            date_string = mi.Get(MediaInfoDLL.Stream.General, 0, u'Recorded_Date')
            if date_string:
                shot_date = dateutil.parser.parse(date_string)
            else:
                date_string = mi.Get(MediaInfoDLL.Stream.General, 0, u'Encoded_Date')[4:]
                if date_string:
                    shot_date = dateutil.parser.parse(date_string).replace(tzinfo=dateutil.tz.tzutc()).astimezone(dateutil.tz.tzlocal())
            mi.Close()
            if not date_string:
                return None
            return shot_date.strftime(self.file_format)
        except Exception as ex:
            logging.warning('%s: %s' % (source_file, ex.message))
            return None
    
    def _get_video_size(self, source_file):
        mi = MediaInfoDLL.MediaInfo()
        mi.Open(source_file)
        non_numbers = re.compile(r'[^\d]+')
        size = (
                int(non_numbers.sub('', mi.Get(MediaInfoDLL.Stream.Video, 0, u'Width'))), 
                int(non_numbers.sub('', mi.Get(MediaInfoDLL.Stream.Video, 0, u'Height')))
                )
        rotation = mi.Get(MediaInfoDLL.Stream.Video, 0, u'Rotation')
        if rotation and ( '90' in rotation  or '270' in rotation):
            size = (size[1], size[0])
        mi.Close()
        return size

    def _get_new_name_from_date_created(self, source_file):
        timestamp = os.path.getmtime(source_file)
        create_date = datetime.datetime.fromtimestamp(timestamp)
        return create_date.strftime(self.failed_file_format)

if __name__ == "__main__":
    try:
        imageMover = ImageMover()
        imageMover.start()
    except Exception as ex:
        logging.error("Error: -  %s" % ex)